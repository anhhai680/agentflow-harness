You run a review wave: the review of {wave_title} of the plan, one reviewer sub-agent per task, at the same time. The runner is the controller: it built these tasks as one wave, and it acts on each task's findings itself (it sends a task with blocking findings back to its own fix session, and commits each task on its own).

**Do not review anything yourself**, and do not edit any file. The review skill named above is your reviewers' brief, not yours: each card carries it. Your only work is to dispatch the reviewers and report what they found:

1. In **one message**, call the `Task` tool once for each review card below, all in that one message, so the reviewers run at the same time. For each card:
   - `description`: `Review Task <n>: <title>`, exactly as the card's line below gives it;
   - `prompt`: "Read `<card path>` and follow it.", with the card's path from its line below;
   - leave `model` out, and `subagent_type` too: every reviewer runs on your model;
   - leave `run_in_background` out, or set it `false`: every reviewer must finish inside its call.
2. Dispatch nothing else: no reviewer for a task that has no card here, no second reviewer for a card, no builder.
3. Wait until every reviewer has finished. Each one ends with a short report: `n`, `findings` and `risks` (lists, maybe empty).
4. Then write your result file with `status: done`, an empty top-level `findings`, and one `tasks` entry per card, with the findings and the risks copied from its reviewer's report as they are:

```json
{"status": "done", "summary": "One line on the wave's review.", "findings": [], "tasks": [{"n": 2, "findings": [{"severity": "blocking", "file": "lib/a.py", "line": 12, "text": "what is wrong and why"}], "risks": []}]}
```

A reviewer that ended with no report, or a report you cannot read, gets no entry: never write one for it, and never make up its findings. The runner then reviews that task in a session of its own.

The review cards:

{wave_cards}
