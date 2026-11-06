---
name: agentflow-debug
description: Use when a step asks you to fix a bug: reproduce it, find the cause before changing anything, then fix it and prove the fix with a test.
---

# Debug: reproduce, isolate, fix, prove

Do not guess at a fix. A change made before the cause is known hides the bug or moves it.

## Steps

1. **Reproduce.** Find the smallest input or sequence that shows the failure, and run it. Note the exact error, output and conditions. If you cannot reproduce it, stop and say what you tried.
2. **Isolate.** Narrow it down: read the code on the failing path, check what changed recently, and compare a case that works with one that does not. Keep going until you can state the cause in one sentence and point to the line.
3. **Write the failing test.** A regression test that fails because of that cause and for no other reason. Run it and see it fail.
4. **Fix the cause**, not the symptom. Make the smallest change that does it.
5. **Prove it.** Run the regression test and see it pass. Run the tests around the code you changed.
6. Report the cause, the fix and the test in your result.

## Rules

- No fix without a cause you can name. If two fixes in a row fail, stop and rethink: the cause is not what you believed.
- Run only the tests you wrote or changed, and the ones next to the code. The runner's gates run the rest. Never commit.
- Do not widen the change: unrelated problems you notice go in your result as notes, not in the diff.
- If the right behaviour is not settled by the docs or the tests, stop and ask.
