Use the `domain-modeling` skill.

Goal: apply the human's answers in `{questions}` for "{topic}" to the design docs, per `CLAUDE.md` "Editing the idea doc".

Update every idea-doc section the decision touches (find them with `grep -n '^## '`; never read it whole). Add a row to the Decisions Log, remove the item from Open Questions, and record a rejected alternative under Not Doing (and Why).
If an ADR covers the decision, add a new ADR that supersedes it, set the old status to `Superseded by ADR-NNN`, and update `docs/decisions/README.md`. Never edit an old ADR's decision in place.
Do not commit: the runner commits.
