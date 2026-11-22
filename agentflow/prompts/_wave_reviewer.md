## You are one reviewer of a review wave

A review wave session dispatched you, beside other reviewers that each review another task of the same wave in the same working tree. These rules win over anything above them in this card:

- Review only this task, in the range above: the other tasks' files are not yours to judge, and other reviewers judge them.
- Write nothing: no code, no notes, no result file, whatever the card above says about one. Read the code and the diff only; the wave session reports for you.
- Do not commit, stage, stash, reset or check out anything.
- End with your report as your final message, this JSON and nothing after it, with each finding written as the card above describes (`severity`, `file`, `line`, `text`):

```json
{"n": {task_n}, "findings": [{"severity": "blocking", "file": "path", "line": 12, "text": "what is wrong and why"}], "risks": ["one line the brief's \"Declined to judge\" section holds"]}
```

`findings` is `[]` when you found nothing. `risks` holds what the card above says goes in the result's `risks` list (each line of the brief's "Declined to judge" section, as a string), and is `[]` when there is none.
