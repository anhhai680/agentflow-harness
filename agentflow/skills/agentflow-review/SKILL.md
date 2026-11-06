---
name: agentflow-review
description: Use when a step asks you to review a change against its plan and spec: label each finding Critical, Important or Minor, and list what you declined to judge.
---

# Review: judge the diff against the plan and the spec

You are a fresh reviewer. Judge the code on its own and change nothing: the implementer fixes findings.

## What to check

1. Does it do what the plan task and the spec say, and no more? Did every task land, and did nothing outside the plan change?
2. Are the tests real? A test that would pass without the change proves nothing.
3. Do the repository's quality rules and agent instructions still hold: no suppressions, no stubs, no skipped tests?
4. Are errors, security and edge cases handled? Check untrusted input, paths, secrets and failure modes.
5. Do the docs the change touches still agree with the code?

## Labels

- **Critical**: broken behaviour, data loss, a security hole, or a requirement missed. Must be fixed.
- **Important**: a real defect or a gap in tests that will cause trouble. Must be fixed.
- **Minor**: style, naming, small clarity points. Does not block.

The runner treats Critical and Important as blocking, and Minor as not.

## Declined to judge

End with a section `## Declined to judge`: one line for each behaviour you considered and set aside as outside the plan or the spec, with the reason. Write "None" if you set nothing aside.

## Output Format

Findings first, each with a label, the file and line, and what is wrong and why. Then the declined list. Then a verdict.

```
Important: app/names:12 - an empty name is accepted, but the spec's criterion 2 says it is refused.
Minor: tests/names_test:30 - the test name does not say what it covers.

## Declined to judge
- Rename of the helper module: not in the plan, a separate change.

Verdict: one blocking finding.
```

If there is nothing blocking, say so plainly. Give a line number for every finding you can place.

When the step asks for a result file, put the findings there instead of this form: Critical and Important as `blocking`, Minor as `minor`, and each "Declined to judge" line as a risk.
