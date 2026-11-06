Goal: apply the human's answers in `{questions}` for "{topic}" to the design docs, per what the repository's agent instructions say about editing the design docs.

Update every idea-doc section the decision touches (find them with `grep -n '^## '`; never read it whole). Add a row to the Decisions Log, remove the item from Open Questions, and record a rejected alternative under Not Doing (and Why).
If a decision record covers the decision, add a new decision record that supersedes it, set the old one's status to `Superseded by <the new record>`, and update the decision records' index. Never edit an old decision record in place.
Do not commit: the runner commits.
