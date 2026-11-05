Use the `security-and-hardening` skill.

Goal: review this branch's diff for security problems, using the `security-auditor` agent on the branch diff (`git diff origin/main...HEAD`).

{earlier_findings}

Judge the code on its own. Do not open or append to `{progress}`, whatever the rules above say about it: it holds the implementers' account of this work.

Focus on this repo's security paths: Entra and IS4 token validation, `customer_code` injection (never a model argument), `visibility = public` filtering, staff-only data on widget routes, attachments, and redaction.
Report each finding with severity, file, line and a fix. Do not edit code. State plainly when a path was reviewed and clean.
Put every finding in the result's `findings` list as `{"severity": "blocking", "file": "path", "line": 12, "text": "what is wrong, why, and the fix"}`; `severity` is `blocking` or `minor`, and no other value: any other severity counts as blocking.

## The skill

This is its full text, so you already have it: do not load it, follow it. Its relative links point into `.claude/skills/security-and-hardening/`.

{brief}
