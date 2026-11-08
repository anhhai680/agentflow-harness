# Engine lessons

Lessons that hold in any project. They ship with the engine, are edited by hand, and come before a pack's own
lessons in every step's prompt. The format is in `lessons.py`: a `## E-NNNN <title>` heading, optional `- key: value`
lines, then one body line.

## E-0001 Grep a big document by heading before reading it
- review_by: 2027-11-08
Run `grep -n '^## '` (or `'^#'`) on a long spec, plan or design doc first, then read only the sections you need with offset and limit; never read one whole, and never read a section twice.

## E-0002 Update the progress file after each sub-step
- review_by: 2027-11-08
Append a 3-5 line entry to the progress file after each sub-step: what is done, the files you touched, what is next and open doubts. It is your only memory if the session is compacted or replaced.

## E-0003 Run only the tests you wrote or changed
- review_by: 2027-11-08
Run the tests you wrote or changed, never the whole suite: the runner's gates run the full checks after the step, so a full run inside the step only spends turns and budget.

## E-0004 Never rephrase a denied command
- review_by: 2027-11-08
A denied shell command stays denied in any other form, and each denied call still costs a turn. Stop as blocked and name the exact command instead of trying variants of it.

## E-0005 Ask instead of guessing a decision the docs do not settle
- review_by: 2027-11-08
When the spec, the plan and the repository's agent instructions do not settle a decision, stop with needs_human and mark the safe option of each question as recommended; do not pick an answer and carry on.

## E-0006 Leave every commit and push to the runner
- review_by: 2027-11-08
Never commit, amend, rebase, push or open a pull request: the runner commits after the gates pass, and a step that moves HEAD is refused.
