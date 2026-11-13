# Proposal: a doubt review of the spec and the plan before a human approves them

Status: approved (2026-11-13)

**Depends on:** none

Source: the method of the vendored `doubt-driven-development` skill (`.claude/skills/doubt-driven-development/SKILL.md`), applied by the runner to the two artifacts a run produces before any code: the spec (`define`) and the plan (`plan`). It reuses the findings machinery that `verify` and `review` already run on (`autonomy.phase_findings`, `blocks_on_findings`, `autonomy.finding_rounds`, refine rounds) rather than building a second copy. The decisions below were settled by grilling on 2026-11-13; see the end of Open Questions.

## Objective

1. **Nothing checks the spec or the plan before a human sees them.** `define` drafts the spec (`draft`), adds contracts (`contracts`), grills open questions (`grill_open_questions`), and then stops at `define_approved`. `plan` writes the plan (`plan`) and stops at `plan_approved`. Both phases have `needs_approval: true`, and `_Loop.settle` (`agentflow/pipeline.py:388`) raises the approval page **before** it looks at findings. No step in either phase has `blocks_on_findings`, so there are no findings to look at. Code has three reviewers (the per-task review, `verify`, `branch_review`), while the artifacts that code is built from have none.
2. **The human approves the author's own account.** The approval page shows the author step's summary, its decisions and its risks: the author's claims about its own work. Nobody tried to disprove them. A spec that contradicts a dependency's contract, a requirement nobody can test, or a plan whose `Interfaces` cite a `path:line` that doesn't exist all reach the human looking just as finished as a sound artifact.
3. **Errors found later cost the most.** A wrong spec decision shows up in `build` as a `spec_gap` stop, as rework across several tasks, or as a blocking `branch_review` finding after every task has been built on it. A plan with a missing task or a bad dependency order shows up as `fix_attempts` and resolver sessions. The doubt skill's rationale holds here: course-correcting is cheapest before anything is built on the artifact.
4. **The only doubt step today is optional and manual.** On a security path, the plan's `approval_note` offers `/doubt-driven-development` for the human to run themselves (`pipeline._approval_note`). The runner never runs it, because the skill is in `guardrails.user_only_skills` and its own Loading Constraints rule out running it inside a subagent or persona. Off a security path, there is no offer at all.

Success:
- Before a human is asked to approve a spec or a plan, a fresh-context reviewer that is told to **disprove** it has already reviewed it.
- Every `critical`, `high` or `medium` finding has gone through an automatic refinement round. Each was fixed, or written down as a trade-off, or flagged as noise with a reason.
- The approval page shows the human what was found, what changed and what is still open.

### What already exists and is reused, not rebuilt

| Need from the doubt method | Already in the engine | This slice adds |
|---|---|---|
| A fresh-context reviewer | `role: review` steps are read-only and always a fresh session, also in a refine round | Two review prompts, `spec_review.md` and `plan_review.md` |
| An adversarial prompt that says "find issues, don't validate" | The review prompts' findings schema, and severities that fail closed (`autonomy.is_blocking`) | Adversarial wording, the severity scale `critical` / `high` / `medium` / `low`, and a bundled skill `agentflow-doubt` as the step's brief |
| Passing the ARTIFACT and the CONTRACT, never the CLAIM | `branch_review.md` already says "Judge the code on its own. Do not open `{progress}`" | The reviewer's prompt carries no author summary, handoff, decisions or code map |
| Findings that hold a phase back | `blocks_on_findings`, `autonomy.phase_findings`, `blocking_of` | These are honoured in a `needs_approval: true` phase **before** its approval is raised |
| A bounded loop | `autonomy.finding_rounds` (2) and the runner-opened refine round (`hitl.open_refine`, `by: runner`) | A **reconcile round**: a refine round scoped to the reconcile step and the reviewers. An engine floor keeps automatic refinement on |
| RECONCILE (classify each finding against the artifact) | `plan_fixes` / `receiving-code-review` already sort review feedback in `verify` and `review` | A `reconciles: true` step that returns a `dispositions` list, one entry per finding |
| STOP (trivial findings, 3 cycles, escalate) | Only non-blocking severities left → approve; rounds used up → human | Two more stops: the artifact didn't change, and "doubt theater" (no actionable finding over 2 rounds) |
| Showing the human | `_review_findings` on the approval page | Each finding's severity, disposition and round |
| Measuring | `signals.py` vocabulary, `scorecard`, `lessons effects` | One signal family, `doubt_*:<phase>` |

## Design

### 1. Where it runs

Two review steps and two reconcile steps, in the `feature` lane only. They are **on by default**:
- they go in the engine defaults (`agentflow/defaults/config.yaml`), so a sparse pack from `agentflow init` gets them
- this repository's pack, which writes its own `phases:`, adds them too
- a pack that doesn't want the review removes the steps

Both phases already stop for a human, so the review adds no new stop. It only adds evidence before an existing one.

```yaml
phases:
  define:
    steps:
      # ... idea, debug, draft, contracts, grill_open_questions as today ...
      - {id: spec_reconcile, lanes: [feature], skill: "@skills.map.design_doc", role: plan, prompt: reconcile.md,
         output: "{spec}", reconciles: true, commit: true, message: "Revise the {module} spec after its review"}
      - {id: spec_review, lanes: [feature], skill: "@skills.map.doubt", role: review, prompt: spec_review.md,
         when: spec_awaits_approval, blocks_on_findings: true}
      - {id: spec_approval, lanes: [feature], gates: [], when: spec_awaits_approval}
  plan:
    steps:
      - {id: plan, lanes: [feature], ...}            # as today
      - {id: plan_reconcile, lanes: [feature], skill: "@skills.map.plan", role: plan, prompt: reconcile.md,
         output: "{plan_target}", reconciles: true, commit: true, message: "Revise the {module} plan after its review"}
      - {id: plan_review, lanes: [feature], skill: "@skills.map.doubt", role: review, prompt: plan_review.md,
         blocks_on_findings: true}

skills:
  map:
    doubt: agentflow-doubt       # new: the bundled skill; a pack may map its own (never a user-only skill)
```

- `spec_review` runs `when: spec_awaits_approval`, which is exactly when a human is about to be asked to approve the spec. A spec that is already `approved` isn't reviewed again. The doubt skill's "When NOT to use" says the same: a decision a human already made isn't re-litigated.
- The reconcile step comes **before** its reviewer in the list, because in a reconcile round it has to run first. On the first pass it doesn't run (see 3).
- **The bug lane is out of this slice.** Its `define` runs `debug`, and its red-then-green expectation is the mechanical doubt step: the doubt skill itself says "a failing test produced by TDD's RED step satisfies this for behavioral claims". Revisit it if bug runs show diagnosis rework: resolver sessions on `red_test_passes`, or repeated fix attempts.
- The `plan` phase's `approval_note` still offers `/doubt-driven-development` on a security path. The automated review is a floor, not a replacement. The interactive skill adds what a run can't do: a cross-model second opinion that the human authorizes.

### 2. The doubt cycle, step by step

| Doubt step | What the runner does |
|---|---|
| **CLAIM** | The author step's `summary` and decisions are the claim. They stay on the approval page and are **never** given to the reviewer. |
| **EXTRACT** | The reviewer gets the ARTIFACT by path and the CONTRACT by path (table below), and nothing else from this run's history. |
| **DOUBT** | One fresh `role: review` session, with an adversarial prompt and the `agentflow-doubt` brief. Every finding has a severity on the scale below. |
| **RECONCILE** | Any `critical`, `high` or `medium` finding makes the runner open a reconcile round. The reconcile step classifies **every** such finding, edits the artifact for the actionable ones, and returns `dispositions`. |
| **STOP** | The runner decides, from files and events, never from a model (section 5). |

**Severity scale.** The doubt reviewers rate each finding `{"severity": "high", "file": "path", "line": 12, "text": "what is wrong and why"}`:

| Severity | Meaning | What the runner does |
|---|---|---|
| `critical` | The artifact can't be built as written, or building it would do harm (a contradiction, a security hole, a missing core requirement) | Opens a reconcile round automatically |
| `high` | The artifact fails its contract in a way the build would hit (a spec requirement no task covers, a wrong interface) | Opens a reconcile round automatically |
| `medium` | A real gap that costs rework later (a failure mode left out, a test plan that doesn't test the claim) | Opens a reconcile round automatically |
| `low` | Worth knowing, with no effect on whether the artifact can be built correctly | Shown on the approval page and in the handoff; not reconciled |

A missing or unknown severity counts as blocking, as it already does (fail closed). The rule needs no new code: `critical`, `high` and `medium` are blocking because they are not in `autonomy.non_blocking_severities` (`[minor, low, info, nit]`), and the floor in section 9 keeps it that way. `task_review` and `branch_review` keep their `blocking` / `minor` scale and their brief's Critical/Important mapping.

What the reviewer reads:

| | ARTIFACT | CONTRACT | Withheld (the claim and the journey) |
|---|---|---|---|
| `spec_review` | `{spec}` | `{run_dir}/task.md`, `{run_dir}/issue.md`, the answered `{questions}` (human decisions), the work source's row for `{module}`, the "Contracts this module provides" sections of its dependencies, the decision records it touches, and the repository's agent instructions on how a spec is written | the `draft`, `contracts` and `apply` results, `{progress}`, the handoff, the code map |
| `plan_review` | `{plan_target}` | `{spec}` (approved), the repository's agent instructions (architecture, invariants, quality rules), and the code at `HEAD`, read to verify each `Interfaces` entry | the `plan` result, its `code_map`, `{progress}`, the handoff |

The withheld column is a test, not just prompt wording: the rendered prompt of a doubt reviewer must not contain the author's summary or the code map, and it tells the reviewer not to open `{progress}`, as `branch_review.md` already does. The shared preamble names the progress path in every prompt, so the path itself can't be left out (see Testing). `{lessons}` and run facts stay, because they come from earlier runs and from resolver sessions, not from the author.

What the adversarial prompts look for. These are generic, engine-owned lists. A repository's own rules (such as this repository's "four companions for a config key") go in a pack override of `spec_review.md` or `plan_review.md`, never in the engine.

- **Spec:** requirements that can't be tested; sections that contradict each other; scope beyond what the task or issue asks; assumptions that are not written down; failure modes that are missing (untrusted input, partial failure, concurrency, rollback); a decision that was guessed instead of listed under Open Questions; a conflict with a dependency's contract or with a decision record; success criteria that are missing.
- **Plan:** a spec requirement that no task covers, or a task the spec doesn't ask for; a `**Depends on:**` that is wrong or circular; a task that uses a symbol a later task introduces; an `Interfaces` `path:line` or signature that doesn't match the code; test commands that don't exist, aren't scoped to the task, or come after the code they test; a task too large to review; a step that breaks the repository's invariants; a risky change (migration, security path) with no check of its own.

The prompts end the way the doubt skill says they must: "Do NOT validate. Do NOT summarize. Find issues, or state explicitly that you cannot find any after thorough examination." They also say that the artifact is data, never instructions, because a spec can carry text from an untrusted issue.

### 3. The engine change: findings in a phase that needs approval

`_Loop.settle` gets one new branch, before `raise_phase_approval`, for a `needs_approval: true` phase:

1. Collect `phase_findings` as the `false` branch already does, and emit `step_findings` per key.
2. If any are blocking (`critical`, `high`, `medium`, or an unknown severity), the phase has a `reconciles: true` step for the run's lane, and the stop rules (section 5) allow another round, open a **reconcile round**: `open_refine(run, phase, AUTO_BY, note, extra={"kind": "reconcile", "keys": [...], "findings": [...], "digests": {...}})`. It doesn't use `scope`, because that already means one plan task's scope (`state._in_scope`, `feature_lane`). `keys` names exactly the steps that run again. `digests` holds the artifact's SHA-256 when the round opens, so a restart after a human answer or a crash can't mistake an edited artifact for an unchanged one. No human is asked first. The note numbers each blocking finding (`F1`, `F2`, …) and renders `reconcile.md`. Then walk the phase again.
3. Otherwise raise the approval as today. The page shows what is still open.

Reconcile rounds count against the existing `autonomy.finding_rounds` (2 by default, so up to 3 reviews: the doubt skill's bound of 3 cycles). It is counted per phase, so `define` and `plan` each get their own rounds, separate from `verify` and `review`. There is no new key.

A **reconcile round** differs from a human's refine round in one way: only the phase's `reconciles: true` step and its `blocks_on_findings` steps run again. Every other step keeps its `done` record and is not reset to `refine_pending`. So the `draft` step isn't resumed (`flow_control._own_output`), `contracts` and the grilling group aren't re-evaluated, and no session is spent answering `unchanged`. A human's rejection still runs every step again with the note, exactly as today, and a `reconciles: true` step never runs in a human's round.

`reconciles: true` means three things:
- The step runs **only** in a reconcile round.
- Its commit may change only its `output`. The runner refuses a reconcile commit that touches any other path. It is an edit to the reviewed artifact, nothing more, so it can never slip a code change past the reviewer.
- Its result must carry `dispositions` (section 4).

`verify` and `review` keep their current refine rounds in this slice (see Follow-ups).

### 4. RECONCILE: dispositions

The reconcile step is a **fresh** `role: plan` session. It doesn't resume the author's `draft` or `plan` session: a fresh session is less anchored on the draft, and it works the same whether or not the author ran in this run (a spec a human wrote, for example). It reads the artifact, the contract, the numbered findings and the dispositions of earlier rounds. For **every** blocking finding, it checks the finding against the artifact text and classifies it in the skill's precedence order:

```json
"dispositions": [
  {"finding": "F1", "class": "actionable",      "note": "Added the rollback case to Failure modes"},
  {"finding": "F2", "class": "contract_misread", "note": "The reviewer was not given ADR-0004, which settles this"},
  {"finding": "F3", "class": "trade_off",       "note": "Kept the single-writer design; recorded under Risks"},
  {"finding": "F4", "class": "noise",           "note": "Task 3 already covers it (its second test)"},
  {"finding": "F5", "class": "human_decision",  "note": "Conflicts with the answer to idea question 2"}
]
```

The classes are tried in this order, and the first that fits wins: `human_decision`, `contract_misread`, `actionable`, `trade_off`, `noise`. This is the skill's order, with `human_decision` first, because a finding against a human's decision is never "fixed" by the reconcile step.

- `actionable`: the step edits the artifact in place. It never adds an Open Question: the grilling step has already run and won't run again in a reconcile round. A decision the docs don't settle becomes a `boundary` question instead.
- `trade_off`: the step writes the trade-off **into the artifact** (its Risks section), so the human sees it next to the decision, not only on a page that is gone after approval.
- `contract_misread`: the reviewer lacked an input. The note names it. The next round's reviewer sees it, and the signal (section 7) lets a retro turn it into a lesson for the review prompt.
- `noise`: correct under context the reviewer didn't have. The note says why.
- `human_decision`: the finding argues against something a human already decided (an answered question, a `**Resolved**` entry, or, for the plan, the approved spec). The reconcile step never reverses such a decision. It asks a `needs_human` question with kind `boundary`, which `autonomy.human_question_kinds` keeps for a human, so the runner never auto-answers it. In the plan, a finding that the spec itself is wrong is also a `boundary` question, never `spec_gap`: the runner auto-answers a `spec_gap` and files it into the approved spec. The question's `id` is the finding's id (`R<n>-F<m>`), so the runner can tie the human's answer to that finding.

A `critical`, `high` or `medium` finding that the reconcile step classifies as `noise`, `trade_off` or `contract_misread`, with its note, counts as refined. The next reviewer may still dispute it.

The runner checks the list, failing closed. A blocking finding counts as **unresolved**, and is listed on the approval page as such, in any of these cases:
- it has no disposition
- its disposition has an unknown class, an id that isn't in the round, or is a second entry for the same id
- it is a `human_decision` with no `boundary` question that a human (not the runner) answered on that step

An `actionable` claim on an artifact that didn't change is shown as "claimed, artifact unchanged", never as fixed. The runner records `findings_reconciled` (`phase`, `round`, `dispositions`, `unresolved`, and the artifact's digest when the round opened and after the step).

The next reviewer gets every earlier round's findings and dispositions since the last human round, as `{earlier_findings}`. It may accept a `noise` or `trade_off` disposition. Or it may raise the finding again with `"disputes": "<id>"` and a reason, which ends the rounds and goes to the human as disputed. A finding never goes round in a loop without a human seeing it.

### 5. STOP: what ends the loop

Refinement is **automatic and bounded**. Every `critical`, `high` and `medium` finding goes to a reconcile round with no human asked first. What ends the loop is decided by the runner, from events and file contents:

| Stop | Rule | Then |
|---|---|---|
| Clean | No blocking finding is left (only `low`, or none) | Approval page, which lists the `low` findings |
| Unchanged artifact | A reconcile step left `output` byte-identical (only `noise`, `trade_off`, `contract_misread` or `human_decision`) | No re-review, because re-reviewing an unchanged artifact is stalling. Approval page with the dispositions |
| Disputed | The re-review raises a finding again with `disputes` | Approval page: the human decides |
| Doubt theater | Two rounds had blocking findings and none was classified `actionable` | Approval page, with a line that says so: the review is validating, not doubting. It is checked before Bounded, so the line shows at the default of 2 rounds |
| Bounded | `autonomy.finding_rounds` reconcile rounds were used (2 by default, so 3 reviews) | Approval page, which says "N blocking findings still open. Approving ships them." |
| Human question | A `human_decision` question is open | The run waits for the human as for any other `boundary` question |

Every rule counts only events since the phase's last human round (a human's rejection, or the phase's start). So a human who rejects the phase after the rounds are used gets fresh rounds for the re-review.

There is no unbounded round for `critical` or `high`: a reviewer and a reconcile step that disagree would loop with nothing to stop them. What is still open after the rounds goes to the human, marked as such.

The run budget (`defaults.run_budget_*`) still caps everything. In the worst case, a phase spends three review sessions and two reconcile sessions. They are judgement steps, so the harness proposal's §6b item 4 effort floor applies: medium or higher, never the `mechanical` model.

### 6. What the human sees

The approval page's existing "Review findings" section (`phase_approval.show: review_findings`) gets, per finding: the round, severity, text, its disposition and note, and whether it is still open, unresolved or disputed. Above it goes one line on what ran: "Doubt review: 3 review(s) and 2 reconcile round(s), single-model; 3 fixed, 1 trade-off(s) recorded, 1 still open." The section shows nothing about a review that didn't run, for example when the spec was already approved. Reconcile rounds don't make the page look like a rejection: the "refine round N" label and the "since the last approval request" sections count only a human's rounds.

It always says **single-model**. Every review in this slice runs on the phase's configured agent. The doubt skill's rule is that skipping a cross-model review is fine and skipping it silently is not, and a run is a non-interactive context, so it never invokes another model on its own. The page always says that a cross-model opinion is the human's to run. When the phase has an `approval_note`, it names that note's skill (in this pack, `/doubt-driven-development`). The engine itself names no user-only skill.

The handoff's Risks list gets the open findings, the `low` findings and the recorded trade-offs, so `build` sees them.

### 7. Measuring whether it's worth it

New signal keys in `signals.py`'s vocabulary, per phase:
- `doubt_blocking:<phase>`: blocking findings in the first review
- `doubt_rounds:<phase>`
- `doubt_actionable:<phase>`, `doubt_noise:<phase>`, `doubt_misread:<phase>`
- `doubt_open:<phase>`: blocking findings still open at the approval

These come from one `doubt_settled` event the runner writes each time a doubt phase raises its approval, and from the `findings_reconciled` events. They aren't count families: a run where no doubt review ran has no `doubt_*` key at all, rather than a 0 that would bias the baseline. `doubt_actionable` is better higher, and the others lower. Reconcile rounds aren't counted in `refine_rounds:<phase>`, and they aren't costed as rework (`metrics`), so the existing baseline keeps its meaning. A retro sees each `contract_misread` and `noise` disposition as evidence for a lesson.

The review is judged against the frozen baseline (`agentflow scorecard`) on the downstream signals it should move:
- human rejections at `define_approved` and `plan_approved`
- `spec_gap` stops in `build`
- `fix_attempts:build`
- blocking `branch_review` findings

It is also judged on its added cost per run. A high `doubt_noise` share means the prompts need tuning, which is a retro's job. It is also the trigger for the resumed-reconciler experiment (see Follow-ups).

### 8. Deterministic pre-checks on the plan (the harness disposes)

Some plan defects need no model. Before `plan_review` runs, the runner checks the plan file itself (`sources.py` already parses plans). The severities follow the plan rules the engine already has:

| Check | Severity |
|---|---|
| A `**Depends on:**` names a task that doesn't exist | `high` |
| The dependencies form a cycle | `high` |
| An `Interfaces` entry written as `path:line` (existing code) whose path is missing at `HEAD`, or whose line is past the end of the file | `high` |
| A task with checkboxes has no `**Depends on:**` line (`plan.md`: the runner then assumes the previous task) | `low` |
| A task with checkboxes names no test command (a docs-only task may have none) | `low` |
| An `Interfaces` entry without a line number (code another task provides), and deferred tasks (no checkboxes) | not checked |

Each failed check becomes a finding with key `plan/plan_review/check`, in the same list as the reviewer's, so a `high` one opens a reconcile round like any other. These checks are cheap and certain, so they go first and the model's attention goes to what only a model can judge.

The spec gets no pre-checks. The engine already handles both of the cases one could check: a missing status line is `spec_is_approved`'s `on_unknown: ask_human`, and a spec with no Open Questions section has none to grill.

### 9. Config keys (each with its four companions)

| Key | Settings (`config_edit.py`) | `validate` | `test_config_edit` case | Playbook §14 |
|---|---|---|---|---|
| `reconciles` (new step key, in `STEP_KEYS`) | protected through `phases.<name>` | must be an agent step, not `role: review`; has an `output`; at most one per phase and lane; comes before the phase's `blocks_on_findings` steps; warns if the phase has no `blocks_on_findings` step (it would never run) | an edit is refused as protected | one line |
| `skills.map.doubt` (new map entry) | protected through `skills` | the skill resolves; refused if it is in `guardrails.user_only_skills` | an edit is refused as protected | one line |
| `blocks_on_findings` (meaning widened) | unchanged | warns on a `needs_approval: true` phase that has `blocks_on_findings` steps but no `reconciles: true` step: its findings go straight to the human | an edit is refused as protected | line updated: "holds back any phase; in an approval phase, the approval" |
| `autonomy.non_blocking_severities` (**engine floor**, changed) | the existing Form field | **error** if it lists `critical`, `high` or `medium` | a proposal that adds `medium` is refused with the floor's message | line updated |
| `autonomy.finding_rounds` (**engine floor**, changed) | the existing Form field | **error** below 1 (today 0 is accepted) | a proposal of `0` is refused with the floor's message | line updated |

The floor is enforced **in code at run time**, because `agentflow run` never calls `validate`:
- the round limit is `max(finding_rounds, 1)`, with an absent value counting as 0;
- `critical`, `high`, `medium`, an empty severity and a missing severity always block, whatever the list says.

`validate` reports an explicit violation as an error. This matters for a pack that writes its own `phases:`, which drops the engine's whole `autonomy` block. Such a pack now gets one runner-opened round on blocking findings where it got none. The rules also cover `verify` and `review`. No pack sets a value the floor refuses today; this pack and the `python` fixture pack set `finding_rounds: 2` and `[minor, low, info, nit]`. A pack that sets a refused value later can't save from the Settings form until its YAML is fixed. The engine-floor lists in `CLAUDE.md` and in the harness proposal §1b get one line each.

`reconciles` must be a boolean, on a top-level `agent` step with `commit: true`, and is refused on nested steps and on `record_step`, `resolve_step` and `retro_step`. `validate` warns about a `reconciles` step in a `needs_approval: false` phase, where it would never run.

Also changed, with no new key:
- the result schema (`hitl.py`) gets optional `dispositions`, which is required from a `reconciles` step
- new prompts `spec_review.md`, `plan_review.md` and `reconcile.md`, and a bundled `skills/agentflow-doubt/SKILL.md`
- the comment on `blocks_on_findings` in this pack. The `python` fixture pack is not edited, because its file pins the `explain` goldens. This pack's edit is a human's, because `.agentflow/**` is forbidden in every run.

No existing prompt text is dropped or reworded, so no `Retired` entry in `pack_fix.py` is needed. If one is, the plan adds it.

### 10. Obligations: the review is evidence at `finish` (PR 3)

Two new engine obligations in the defaults:

```yaml
policy:
  obligations:
    spec_reviewed: {lanes: [feature], require: ["reviewed:define"]}
    plan_reviewed: {lanes: [feature], require: ["reviewed:plan"]}
```

They use a new evidence kind, `reviewed:<phase>`. The existing `step:` evidence doesn't fit: `conformance._step` treats a review as stale after **any** later code change, and `build` always changes code. `reviewed:<phase>` is met when either:
1. the phase's doubt review finished in this run, and either was clean or a human approved the phase after its findings (the same human-override rule as `conformance._step`), or
2. the review never had to run: its step was skipped by a condition the runner evaluated (a spec that was already `approved`), or the phase was skipped through `skip_if` (an existing plan with unticked tasks). This mirrors how `conformance._approval` treats `skip_if` as "on base".

There is **no staleness check**. After the review, the only edits to these artifacts are `record_step` filing a human's `spec_gap` answer into the spec, and ticked boxes in the plan. Neither reopens the review, and a staleness check would fail every run that had a `spec_gap`.

These are ordinary obligations, removable with `null` like `plan_approved`, not part of the engine floor. They follow `policy.enforce` (`report` by default). `validate` checks the new evidence kind, and playbook §14 gets a line for each obligation.

## Boundaries

- **Always:**
  - The reviewer is a fresh, read-only `role: review` session that gets the artifact and the contract and not the claim.
  - Every `critical`, `high` and `medium` finding goes to a reconcile round automatically, within `finding_rounds`.
  - Every blocking finding is classified.
  - Trade-offs are written into the artifact.
  - The runner decides when to stop.
  - The approval page says what was reviewed, how (single-model), and what is still open.
- **Ask first (a human):**
  - Any finding that argues against a human's decision (`human_decision`, kind `boundary`).
  - Disputed findings.
  - Anything still open after the rounds are used.
  - Approving the phase, as today.
- **Never:**
  - The runner never runs `doubt-driven-development` itself (user-only, and its Loading Constraints forbid it).
  - It never calls a second model it wasn't configured to call.
  - A reconcile step never changes anything but its artifact, and never reverses a human's decision.
  - No layer can switch automatic refinement off: the engine floor in section 9.
  - The review never replaces the human's approval of `define` or `plan`. Those stay `needs_approval: true` in the defaults and this pack. Turning `plan`'s approval off still makes `validate` warn about the `plan_approved` obligation. That is a warning, not a refusal, as it is today.
  - No project wording in the engine's prompts or the bundled skill (`test_portability.py`).
  - The `explain` golden files don't change: the `python` fixture pack writes its own `phases:` and doesn't take these steps.

**Security note.** The reconcile step's path restriction is enforced where commits are checked. If that lands in `guards.py`, the change triggers this pack's security overlay. The plan is then reviewed with `security-and-hardening` and the `security-auditor` agent.

## Testing

All tests use `fake_agent.py`, on the `python` fixture pack with the four steps added in a temp copy (never in the golden-pinned pack itself), and on the engine defaults through a sparse pack.

- **Severity:** a `low` finding raises the approval at once, with no reconcile round, and the page lists it. Each of `critical`, `high`, `medium` and a missing severity opens a reconcile round with no human asked first.
- **One reconcile round:** a `high` finding opens a round with `kind: reconcile`. Only `spec_reconcile` and `spec_review` start. `draft` and `contracts` keep their `done` records and get no new session. The second review is clean, so the approval is raised, and the page shows the finding as fixed in round 1.
- **Path restriction:** a reconcile step that edits a file other than its `output` is refused, and its commit doesn't happen.
- **Fail closed:** a missing disposition, or an unknown `class`, makes the finding unresolved and puts it on the page.
- **Unchanged artifact:** all findings are `noise`, the artifact blob doesn't change, and no third review runs.
- **Doubt theater:** two rounds with blocking findings and no `actionable` disposition stop with the theater line.
- **Bounded:** after `finding_rounds` rounds, the approval is raised with "still open" and the count.
- **Human decisions:** a `human_decision` disposition raises a `boundary` question that the runner doesn't auto-answer, even with `answer_recommended: true`.
- **Withheld context:** the rendered `spec_review` and `plan_review` prompts contain neither the author's `summary` nor the code map, and they tell the reviewer not to open `{progress}`.
- **Human rejection is unchanged:** a human rejection after a review reruns every step, the reconcile step stays skipped, and the reviewer runs again fresh.
- **Engine floor:** `validate` errors on `non_blocking_severities` with `critical`, `high` or `medium` at any layer, and on `finding_rounds: 0`. `test_config_edit` refuses both from the Settings page.
- **Signals:** `doubt_*` signals are computed from a fixed event list (`test_signals.py`).
- **Config:** `validate` covers each rule in section 9, and `test_config_edit` covers each protected key.
- **Pre-checks** (PR 2): a dependency on a missing task, a cycle, and a `path:line` past the end of a file each give one `high` finding. A missing `Depends on` and a task with no test command each give a `low` one. An `Interfaces` entry without a line number, and a deferred task, give none.
- **Obligations** (PR 3): `reviewed:<phase>` is met by a clean review; by a human approval after open findings; by a review skipped through `spec_awaits_approval`; and by a `plan` phase skipped through `skip_if`. It is still met after a `record_step` commit to the spec. It is unmet when the steps were removed but the obligation kept.
- **Existing behaviour:** the `verify` and `review` phases' tests pass with no edits. `test_portability.py` and the `explain` golden test pass.

## Rollout

1. **PR 1, the cycle:**
   - the `settle` branch, reconcile rounds, `reconciles`, `dispositions` and the stop rules
   - the severity scale
   - the engine floor on `non_blocking_severities` and `finding_rounds`
   - the three prompts and the bundled `agentflow-doubt` skill
   - the approval page and the signals
   - the engine defaults' four steps, and this pack's four steps
   - playbook §7 (lane recipes) and §14, the engine-floor lines, and the CHANGELOG
2. **PR 2, the plan pre-checks** (section 8).
3. **PR 3, the obligations** (section 10), in `report` mode.

Each PR is measured on the section 7 signals over the first feature runs before the next one starts.

## Risks

- **Cost.** Up to five extra sessions in each of two phases, on the strong model. The bound is `finding_rounds`, the unchanged-artifact stop and the run budget. The signals show the median cost added per run. If that is out of proportion to the rejections and rework it saves, a pack can lower `finding_rounds`, but never below the floor of 1.
- **Noise and churn.** A reviewer that is told to find issues will find some. A reviewer that rates a nit as `medium` costs a round. The `low` severity, the reconcile step's `noise` class, the theater stop and the `doubt_noise` signal are the counterweights.
- **The reviewer re-opens settled decisions.** This is the reason for the `human_decision` class: such a finding goes to a human as a `boundary` question, and nothing in the artifact changes on its own.
- **Withholding is wording, not a sandbox.** The agent's workspace mirrors the handoff and earlier step results (`workspace.prepare`). A doubt reviewer's prompt carries neither and tells it not to read them, as `branch_review` does today, but it could still open them.
- **Same model, shared blind spots.** A fresh session with the same model catches anchoring, not model-level blind spots. The page always says "single-model", and the human keeps the cross-model offer.
- **The floor binds `verify` and `review` too.** A pack that wanted to ship `medium` code-review findings without a round can no longer do so. No pack does today.
- **A spec that changes after review.** `grill_open_questions` and `apply` run before the review. A human's refine round reruns the review. The reconcile round's own edits are re-reviewed unless the stop rules end the loop, and then they are shown as changed since the last review.

## Follow-ups (not in this slice)

- **Cross-model review in a run:** a step-level `agent:` key, so a pack can put `plan_review` on Codex with its config as the standing permission. Its own slice, once a Codex `role: review` run is proven.
- **Reconcile rounds for `verify` and `review`,** so that a runner-opened round there reruns only the fixers and the reviewers. Measured against the scorecard baseline.
- **A resumed reconciler:** resume the author's session instead of a fresh one, tried if `doubt_noise` runs high.
- **The bug lane's diagnosis,** if bug runs show diagnosis rework.

## Open Questions

None.

**Resolved (2026-11-13, grilling)**
1. The bug lane is out of this slice. Its red-then-green check is its doubt step. Revisit it on diagnosis rework (section 1).
2. `spec_reviewed` and `plan_reviewed` become ordinary engine obligations with the evidence kind `reviewed:<phase>`, no staleness check, in PR 3 (section 10).
3. Single-model only, announced on every approval page. Cross-model stays the human's `/doubt-driven-development` and becomes a later slice (sections 6, Follow-ups).
4. The reconcile step is a fresh session. A resumed author session is the experiment to try if `doubt_noise` runs high (section 4).
5. Reconcile rounds share `autonomy.finding_rounds`, with no new key (section 3).
6. `verify` and `review` keep their current rounds. Moving them is a follow-up (section 3, Follow-ups).
7. On by default for the feature lane: in the engine defaults and in this pack (section 1).
8. The proposal lives at `docs/agentflow-doubt-review-proposal.md`, so it is a module a run can build.
9. Plan pre-check severities follow the plan rules the engine already has: `high` for a missing task, a cycle or a wrong `path:line`; `low` for a missing `Depends on` or test command; entries without a line number and deferred tasks are not checked (section 8).
10. No spec pre-checks: the engine already handles both cases (section 8).
11. The doubt reviewers use `critical` / `high` / `medium` / `low`. `task_review` and `branch_review` keep `blocking` / `minor` (section 2).
12. Every `critical`, `high` and `medium` finding goes to a reconcile round automatically, bounded by `finding_rounds`. A reconcile step's reasoned `noise`, `trade_off` or `contract_misread` counts as refined, and a `human_decision` finding goes to the human (sections 4 and 5).
13. Automatic refinement is an engine floor: `validate` errors on `non_blocking_severities` with `critical`, `high` or `medium`, and on `finding_rounds` below 1 (section 9).

**Revised (2026-11-13, doubt review of the PR 1 plan)**
- The floor is enforced in code at run time, not only by `validate`. An absent `finding_rounds` gives one round (section 9).
- A reconcile round is `kind: reconcile` with `keys` and `digests`, not `scope` (section 3).
- Dispositions are tried in this order: `human_decision`, `contract_misread`, `actionable`, `trade_off`, `noise`. A reviewer can dispute a disposition with `disputes`, and the stop rules count from the last human round (sections 4 and 5).
- The `doubt_*` signals come from `doubt_settled`, and reconcile rounds are neither refine rounds nor rework (section 7).
- `validate` warns but doesn't refuse `needs_approval: false` on `plan`; this pack's edit is a human's; the `python` fixture pack is not edited (Boundaries, section 9).
- In the plan phase, a finding that the spec itself is wrong becomes a `boundary` question, not `spec_gap`, because the runner auto-answers a `spec_gap` and files it into the approved spec. Finding ids are `R<n>-F<m>` for the whole phase. A reconcile edit that turns a review's `when` false, or makes another step due (such as the open-questions grilling), stops for a human (`reconcile_changed_conditions`). A pack that drops `review_findings` from `phase_approval.show` also drops the doubt lines from its page (trade-off).
