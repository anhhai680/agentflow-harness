# Unified Agent Skills for Tiva Care (v3)

This replaces `agent-orchestration-guide-v2.md`. It merges three skill packs: `obra/superpowers`, `addyosmani/agent-skills` and `mattpocock/skills`. The result is **one skill per SDLC stage**, adapted to this repo. That gets the benefit of all three without paying for their overlaps in tokens, cost or conflicting instructions.

The canonical rules live elsewhere. This guide explains them:
- `CLAUDE.md` → **Agent workflow**: stages, lanes and conflict rules. The agent reads this every session.
- `.claude/skills/VENDORED.md`: where each skill came from, local edits, and how to re-sync.
- `.claude/skills/next-step/SKILL.md`: the handoff map.

## 1. Why v2 didn't work

| v2 assumption | What's actually true (checked against the installed plugins) |
|---|---|
| A precedence table stops collisions | The model picks skills by their descriptions. With all three plugins on, about 95 skills load, including 3 TDD, 4 code-review and 4 debugging skills, so the table can't stop the wrong one firing. `skillOverrides` doesn't apply to plugin skills (only managed policy can set it), so duplicates can't be turned off one by one. |
| `mattpocock:` prefix | It's `mattpocock-skills:`. |
| The agent runs `grill-with-docs`, `to-tickets`, `improve-codebase-architecture` | They're `disable-model-invocation: true`, so only a human can type them. |
| `npx skills add` for all three | Not namespaced: same-named skills (`test-driven-development`) overwrite each other. |
| Startup hooks collide | Only superpowers wires one: a `SessionStart` hook on startup, clear and compact that injects `using-superpowers` ("1% chance means you MUST invoke", "brainstorming first"). addy's hook isn't wired on Claude Code, and mattpocock has none. |
| Combine superpowers TDD with addy's tables | Two TDD skills load for one job (about 8.5k tokens). Superpowers already has the table. |
| Phase 5 adds 5-axis review, personas, simplify and an architecture survey | The plan executor already reviews each task and the whole branch. Stacked reviews mostly re-find the same issues. |
| ADRs and specs go wherever each skill likes | mattpocock writes `docs/adr/0001-*`, superpowers `docs/superpowers/{specs,plans}`. This repo uses `docs/decisions/ADR-NNN-*`, `docs/specs/`, and puts the idea doc first. |
| A 6-phase code pipeline for every task | Most current work is idea-doc refinement, and it needs a lane of its own. The RAG eval gate wasn't covered at all. |

## 2. How the unified set resolves conflicts

1. **The three plugins are off for this repo** (`.claude/settings.local.json` → `enabledPlugins: false`), and so is the synced `engineering@synced` plugin, which overlapped with code-review, debug, testing and architecture skills. That removes every duplicate description and the superpowers hook. Other projects are unaffected.
2. **One winner per stage is copied into `.claude/skills/`** from the pinned installed versions (all MIT; notices in `.claude/skills/LICENSES/`). Plugin prefixes were stripped, references to dropped skills were rewritten, and paths now follow repo conventions.
3. **CLAUDE.md replaces the hook as the router.** It loads every session and survives compaction, and project instructions outrank skills. It names the stage owners, the lanes and the conflict rules.
4. **Heavy or rare skills are user-only** (`skillOverrides: "user-invocable-only"`): the agent doesn't load them on its own, and you type `/name` when you want one.

## 3. The unified set

| Stage | Owner skill (source) | Mode | Duplicates dropped and why |
|---|---|---|---|
| Define | `grilling` (mattpocock) | auto | superpowers brainstorming (writes its own spec/plan trail), addy interview-me / idea-refine, grill-me, grill-with-docs (user-only, and its ADR format clashes) |
| Define: terms + ADRs | `domain-modeling` (mattpocock, adapted) | auto | addy documentation-and-adrs, engineering:architecture |
| Architecture survey | `improve-codebase-architecture` (mattpocock) | user-only | — |
| Define: spec | `spec-driven-development` (addy, adapted) | auto | mattpocock to-spec (tracker-based), brainstorming's spec output |
| Define: contracts | `api-and-interface-design` (addy) | auto | engineering:system-design |
| Quality bar | `constraint-driven-development` (addy) | user-only | — (run once when the first code root exists) |
| Plan | `writing-plans` (superpowers, adapted) | auto | addy planning-and-task-breakdown, mattpocock to-tickets (no tracker yet) |
| Plan: module design | `codebase-design` (mattpocock), loaded by `writing-plans` for a new package | auto | — |
| Build | `executing-plans` / `subagent-driven-development` (superpowers) | auto | addy incremental-implementation, mattpocock implement / implement-spec, dispatching-parallel-agents |
| Build: tests | `test-driven-development` (superpowers) | auto | addy TDD, mattpocock tdd, engineering:testing-strategy |
| Debug | `systematic-debugging` (superpowers) | auto | addy debugging-and-error-recovery, mattpocock diagnosing-bugs, engineering:debug |
| Verify | `verification-before-completion` (superpowers) | auto | — |
| High stakes | `doubt-driven-development` (addy) | user-only | — |
| Security | `security-and-hardening` (addy) + `security-auditor` agent | auto | engineering:code-review's security pass |
| Review | `requesting-code-review` + `receiving-code-review` (superpowers) | auto | addy code-review-and-quality, mattpocock code-review, engineering:code-review |
| Console UI | `frontend-ui-engineering` (addy) | auto | — |
| Ship | `finishing-a-development-branch` (superpowers, adapted) | auto | addy shipping-and-launch and ci-cd-and-automation (deploy is deferred past the MVP) |
| Isolation | `using-git-worktrees` (superpowers) | user-only | addy git-workflow-and-versioning (CLAUDE.md sets commit style) |
| Agent docs | `writing-for-agents` (mattpocock) | user-only | superpowers writing-skills |
| Where am I? | `next-step` (local) | auto + `/next-step` | — |

Left out entirely: addy source-driven-development (the **context7** MCP does library docs), code-simplification (built-in `/simplify`), observability, performance and webperf, deprecation-and-migration (ADR-015 covers migrations), context-engineering, and mattpocock research, prototype, triage, handoff and the rest. Bring one back by copying it in and adding a row to `VENDORED.md`.

## 4. Lanes

Pick the lane from the task, and skip the stages it doesn't list. The full text is in `CLAUDE.md`.

| Lane | Path |
|---|---|
| Design-doc change (Define) | `grilling` → `domain-modeling` → idea doc (all touched sections) → ADR if expensive to reverse → commit when asked |
| New module / feature (Define → Plan → Build → Verify → Review → Ship) | `spec-driven-development` → (`api-and-interface-design`) → `writing-plans` → `executing-plans` or `subagent-driven-development` with TDD → `verification-before-completion` → `finishing-a-development-branch` |
| Bug | `systematic-debugging` → `test-driven-development` (regression test) → `verification-before-completion` → `requesting-code-review` |
| Prompt / model / chunking / embedding | Build without TDD for prompt text → Verify = eval suite, retrieval scored separately → a regression blocks |
| Security paths | add `security-and-hardening` at Plan and Review, the `security-auditor` agent at Review, and offer `/doubt-driven-development` before Build |
| Milestone | `/improve-codebase-architecture`; `/constraint-driven-development` once when code starts |

## 5. Handoffs: you don't have to remember 21 skills

- Every skill ends with a `## Next step` section, so the agent finishes its message with one line, e.g. `Next: writing-plans — the spec is approved (lane: feature)`.
- `/next-step` reads `git status`, the newest plan and the specs, then answers `Lane · Stage · Evidence · Next`. Use it when resuming in a new session.
- The full map is in `.claude/skills/next-step/SKILL.md`. Each footer repeats only its own row.

## 6. Where artifacts go

| Artifact | Location |
|---|---|
| Decisions | the idea doc's Decisions Log first; then `docs/decisions/ADR-NNN-short-title.md` + README index |
| Glossary | root `GLOSSARY.md` (English code name + Vietnamese UI term) |
| Specs | `docs/specs/SPEC-<module>.md`, `docs/specs/CAPABILITY-MAP.md` |
| Plans | `docs/plans/YYYY-MM-DD-<feature>.md` |
| Build scratch ledger | `.superpowers/sdd/<plan>/` (git-ignored by the skill's script) |
| Quality bar | `CONSTRAINTS.md`, once there is code |

## 7. Cost notes (estimates)

- The listing goes from about 95 skill descriptions (about 4k+ tokens) to 22. The superpowers SessionStart injection (about 0.8k tokens at every start, `/clear` and compaction) is gone.
- The bigger saving is in behaviour: no "invoke at 1%" speculative loads (2–13k tokens each), one TDD skill, and one review per task plus one per branch instead of about four review layers.
- `subagent-driven-development` keeps its model-selection rules (cheap models for mechanical tasks). Plans of 3 tasks or fewer run inline with `executing-plans`.
- A plan task is one testable slice ending in one commit. Every task costs a fresh implementer and reviewer, so don't over-split.

## 8. Setup for another developer or machine

`.claude/settings.local.json` is git-ignored (globally on this machine), so the plugin switches are per developer. Anyone with the three plugins installed should add:

```json
"enabledPlugins": {
  "mattpocock-skills@claude-plugins-official": false,
  "superpowers@claude-plugins-official": false,
  "agent-skills@addy-agent-skills": false,
  "engineering@synced": false
}
```

To enforce this for everyone, move the block into the committed `.claude/settings.json`. The `skillOverrides` lines for the user-only skills go in the same place.

## 9. Check that it's working

- `/plugin` (or `claude plugin list`) shows the three plugins as disabled.
- In a new session, `/skills` lists the 22 project skills and no `superpowers:`, `agent-skills:`, `mattpocock-skills:` or `engineering:` entries. The first context has no "You have superpowers" text.
- `/next-step` in this repo reports the design-doc lane.
- A doc-decision request writes to the idea doc and `docs/decisions/`, never `docs/adr/` or `docs/superpowers/`.
- If a session picks the wrong skill, tighten that skill's `description` or the CLAUDE.md lane. Don't add another router.

## 10. Keeping it current

Upstream updates no longer arrive automatically. Re-sync with the steps in `.claude/skills/VENDORED.md` (update the plugin cache, diff, re-apply the listed local edits, bump the version). If a stage's owner changes, update `CLAUDE.md`, `VENDORED.md` and `next-step` together.

## 11. Automated runs

`agentflow` (`devtools/agentflow/`) runs one main workflow without you typing each skill: `define → plan → build → verify → review → ship`. Each phase has several steps, and each step maps to one skill. A lane (`feature`, `bug`, `eval`, `design`) names the kind of work and selects which steps run, so a phase with no step for the lane is skipped with no approval. It is a local Python CLI, `agentflow <command>` (`devtools/bin/agentflow`). Each agent step is one headless `claude -p` call with the step's skill and prompt. It writes a `result.json` and exits. The skills and stage owners in section 3 do not change; the runner only walks them. The design is in `docs/agentflow-proposal.md`, and every behaviour below lives in `devtools/agentflow/config.yaml`.

### What the runner adds

- **Phase approvals.** Every phase says `needs_approval: true` or `false` in `config.yaml`, and the runner only reads that. `define` and `plan` are `true`: the run stops at `define_approved` and `plan_approved`, and nothing starts the next phase until you approve. `build`, `verify`, `review` and `ship` are `false`: they move on by themselves once nothing blocks them, and the card moves with them. `--to define` stops a run after define's approval, which is how you write or amend only a spec.
- **Findings.** `verify`, `branch_review` and `security_review` carry `blocks_on_findings: true`, so their blocking findings hold the phase back. Findings fail closed: only a severity in `autonomy.non_blocking_severities` lets a phase go on, so a reviewer's `medium`, or no severity, blocks. The runner opens a refine round itself: `plan_fixes` (on Opus, read-only) checks each finding against the code and writes the fix plan, `fix_findings` carries it out under the verify gates, and the phase's checks run again. After `autonomy.finding_rounds` rounds, the phase stops at its approval page with the open findings listed.
- **The resolver.** A stop in a phase with `resolve: true` goes to the runner before you hear of it. Kinds in `autonomy.resolve_kinds` get a fresh Opus session (`<step>/resolve-<n>`) that finds the cause, under the same guards and gates, and then the step's own session resumes with its diagnosis. A question whose options all include a recommended one takes it, and a `spec_gap` answer is still recorded in the spec. An edit to a forbidden path is put back once and the step is told why. The step's own gates and reviews still decide whether it is done, and the runner never accepts open review findings: that takes a human.
- **What still stops for you, and notifies:** fixes used up on a blocking problem, the budget, a guard that trips again or isn't a forbidden edit (an agent commit, a rejected trailer, a user-only skill, foreign commits), the agent service being down, a step that keeps running out of context, a condition that can't be decided, a question with no recommended option, a resolver that is itself blocked, a failed push or PR, and `ci_failed`.
- **CI.** After the draft PR, the `ci` step polls `gh pr checks`. When every check passes it refreshes the PR body and runs `gh pr ready`. A failed check starts a fix session on Opus with the failed jobs' logs, which can't edit `.github/workflows/`; the runner commits and pushes the fix and checks again, up to `ci.fix_rounds` times. A session that changes nothing means a flake or an infrastructure failure, so the runner runs the failed jobs again, up to `ci.rerun_rounds` times. **The runner never merges**: every lane ends with a PR ready for your review (`design` makes its one commit first), and a test fails if any merge call appears.
- **The PR is the audit trail.** You don't see each phase any more, so the PR body lists the decisions the runner took, the stops it settled, the automatic fix and CI rounds, and every finding left open.
- **Ready.** A phase approved on the dashboard (or with `agentflow approve --no-continue`) rests as `ready`: its steps are done, a human approved it, and nothing runs. You then decide. **Continue** starts the next phase, and also lifts a `--to` the run started with. **Mark done** skips the remaining phases and goes to `ship`, so a run stopped early still ends with a PR, which lists the phases that did not run. `agentflow approve` without `--no-continue` goes straight on, as it always has.
- **API design before the plan.** `api-and-interface-design` runs once per spec, in `define`, and its questions are grilled before `plan`. The runner knows a spec was reviewed from a `**Resolved (YYYY-MM-DD, API review...)**` list in its Open Questions (`spec_api_reviewed`); a spec with routes, WebSocket messages or provided interfaces and no such list runs the review (`spec_needs_api_design`). `agentflow explain feature --module <m>` shows which.
- **Gates.** Shell commands copied from `CONSTRAINTS.md` (`gates:` and `gate_sets:`) run after the steps that name them. A failing gate resumes the same session with the gate output, up to `defaults.fix_attempts` times, then goes to the resolver in a `resolve: true` phase, or stops for you. A gate whose `when_exists` target is missing is reported skipped, never passed.
- **Batches of small tasks.** The build step's `batch` (`max_tasks: 3, max_files: 3`) groups neighbouring plan tasks that each name at most `max_files` files, and whose dependencies are in the batch or before it (`sources.plan_batches`). It decides the batches once per run (a `plan_batched` event). A batch's tasks share one implement session: the second and later tasks get the short `task_next.md` prompt. Each task is still gated and committed on its own, with its own ticks. Only the last task runs a review, and it covers the whole batch from where its first task started; a fix goes into the last task's commit. A batch task's `can_continue_without` question stops the run instead of parking, so the batch stays whole for that review. Refine rounds and small plans don't batch.
- **Refine rounds.** `agentflow reject <run> <phase> --note "..."` re-runs the whole phase: each step resumes its own session with your note, and the changes land as new `Refine ...` commits. History is never rewritten. The runner then asks for approval again.
- **Effort choice.** Every step's first session starts at `medium`. The agent may ask to start again at `low` or `high` before doing any work. The runner records the level and passes `--effort` on every call, including resumes.
- **Guards.** A step fails if it touches `guardrails.forbidden_paths` (`CONSTRAINTS.md`, `devtools/floor_guard.py`, `eval/baseline.json`, the agentflow config, `.claude/**`) or moves `HEAD`. The runner is the only thing that commits and pushes in a run. It never adds `Constraints-Approved-By`.

### Running and answering

```
agentflow explain feature --module crm          # what would run now, and why steps are skipped; no agent calls
agentflow run feature --module crm --dry-run    # the agent and gate commands; creates nothing
agentflow run feature --module crm              # start; stops at the first approval or question
agentflow run feature --module crm --from plan --to build
agentflow run feature --module crm --model plan=sonnet   # one phase's model, this run only
agentflow inbox                                  # every run waiting on you, with its questions
```

Lanes are `feature`, `bug`, `eval` and `design`. `run` takes `--module`, `--topic`, `--issue`, `--log`, `--task` and `--slice` as the lane needs them.

| Work | Command | Phases that run | Approvals |
|---|---|---|---|
| Feature | `agentflow run feature --module crm` | define, plan, build, verify, review, ship | 2 |
| Spec only | `agentflow run feature --module crm --to define` | define | 1 |
| Bug | `agentflow run bug --issue 42` | define, build, verify, review, ship | 1 |
| Eval | `agentflow run eval --task "…"` | build, verify, review, ship | 0: the eval gate still blocks a regression, and you merge the PR |
| Design | `agentflow run design --topic "…"` | define, ship | 1 |

| You do | Command | What happens |
|---|---|---|
| Approve a phase | `agentflow approve <run> <phase>` | The next phase starts in a fresh session. Only `approve` clears a phase approval. With `--no-continue` the phase rests as `ready`, as a dashboard approval does. |
| Continue a ready run | `agentflow resume <run>` | Starts the next phase, past any `--to`. On the dashboard: **Continue**, or drag the card to the next column. |
| Mark a ready run done | `agentflow resume <run> --mark-done` | Skips the remaining phases and runs `ship`: push and draft PR. Refused with no commits and no changes to ship. On the dashboard: **Mark done**, or drag the card to Done. |
| Reject a phase | `agentflow reject <run> <phase> --note "..."` | A refine round re-runs every step of that phase, then asks again. The note is required. Rejecting `review` runs `plan_fixes` and `fix_findings` first, because the review steps are read-only: they fix the code, the gates run, and fresh review sessions check the branch again, given the earlier blocking findings and your note. It is the same round the runner opens by itself on blocking findings. |
| Clear an inner checkpoint | `agentflow approve <run> <checkpoint>` | For a repeat group past `max_rounds`. |
| Resume a runner stop | `agentflow approve <run> <step-key>` | For blocked, fix attempts exhausted, timeout, budget or `ci_failed` stops. Approving `ci_failed` gives fresh CI rounds. The step's session resumes, then the runner still verifies and commits it. Approving a budget stop allows one more budget's worth of spend, then the runner asks again; add `--budget <usd>` (above the spend so far) to set the limit yourself. Add `--note "..."` to tell the resumed agent what changed or what to do (the dashboard asks for it on a blocked stop). A blocked inbox lists the commands that were denied and where to allow them. If the session blocks again with nothing changed (`blocked_repeat`), a plain approve is refused: fix the cause, then use `agentflow retry` for a fresh session, or approve with `--note`. |
| Answer questions | `agentflow answer <run> --set Q1="..."` | Repeat `--set` per question, or edit the `Answer:` lines in `runs/<run>/inbox/<key>.md`. `--key` answers a parked step; `--record` writes the answer into the spec as a decision. |
| Run a step again | `agentflow retry <run> [step-key]` | A fresh session; the default is the run's failed step. |
| Pause | `agentflow pause <run>` | No step starts after the one running; a live agent finishes its step. `agentflow resume <run>` continues. |
| Cancel | `agentflow cancel <run>` | The run ends and its live agents are stopped. This cannot be undone. |

`approve`, `reject`, `answer` and `retry` record the action and then continue the run in the foreground. `--detach` continues it in the background, and `--no-continue` only records it. A foreground runner outlives its terminal: if the window closes, it carries on and writes to `runs/<run>/runner.log`. `pause` and `retry` refuse a ready run, which has nothing to pause or redo. `agentflow resume <run>` continues a run that stopped for any reason, and first waits for any agent that outlived its runner, so two sessions never share the files.

A run rides out a usage limit or an outage by itself. It shows as `held`, keeps its queue slot, and notifies you once. A usage limit waits for its reset time with no cap. An outage retries with a backoff of up to 15 minutes for `recovery.give_up_h` hours, and a call that reached the model and still died stops after `recovery.failed_attempts`. Both of those end in an `agent_unavailable` stop, which `agentflow approve <run> <step-key>` resumes. `pause` and `cancel` still work during a hold. A runner that dies during a hold needs `agentflow resume`; nothing restarts a dead runner. A second approve of the same phase fails with "not awaiting approval", and a second runner on one run is refused by the run lock. `agentflow attach <run>` opens the waiting step's session interactively in its worktree.

### Starting work from the dashboard (Backlog)

Nothing starts by itself. A developer pulls a card from **Backlog** into the first phase it has work in, and the run starts at that moment. Any number of independent cards can run at once, and each run has its own detached process, worktree, branch, lock and event log.

- **What fills Backlog.** The capability-map modules that have no run (the map file is `project.docs.capability_map`), and the cards you add with **+ New task**. Added cards live in `devtools/agentflow/runs/backlog.jsonl` (an append-only event log, git-ignored), so they stay on that machine and survive a dashboard restart; another developer sees the work only once it is a branch or PR.
- **The mark on each card.** A green **P** (tooltip "Can run in parallel") means nothing blocks the card and at least one other unfinished item with no dependency path to or from it is unblocked or running. A line names them: `Parallel with: B`. **Ready** means nothing blocks it, but nothing can run beside it right now. **Blocked** means `Runs after: A (PR open)`, listing each unmerged dependency and where it stands; a blocked card cannot be dragged and has no **Start**, and the server answers 409 if you post a start anyway.
- **Dependencies.** A module's dependencies come only from the map's `Depends on` column. A dependency is met when its pull request is merged, not when the card is Done (a Done card only means the PR is open), because the next run forks from the base branch and needs that code there. A failed or cancelled run keeps its dependents blocked, with the reason on the card. A card you add declares `after` in the form (other cards, live runs, unmerged modules); with none, it has no declared dependency. Nothing compares the files two runs will touch, so overlapping work needs **Runs after**, set by you.
- **Starting.** Drag the card to its first phase column, or press **Start**. That column is the first phase the card's lane walks, except that a capability-map module moves to the first phase its run has work in: a module with a finished spec (nothing left for any `define` step) goes straight to **Plan**, so nobody re-defines it. A spec that says "pending approval" keeps the card on **Define** (a spec with no status line does not): the run spends nothing there and stops for one phase approval, and that approval is what lets it reach `plan`. The run never edits the spec, so the status line stays as it is until a person changes it, and until then a later run asks again. Only that column highlights as the drop target, and Done never accepts a Backlog card. It is the same run either way: the run still evaluates each phase itself, so if a spec has gained an open question it stops in `define`. The server re-checks the rules, answers 202, and starts `flow.py run … --detach` on a worker thread, one start at a time. A card that fails to start stays in Backlog with the reason, and **Start** tries again. A start interrupted by a dashboard crash is marked failed on the next start and never repeated.
- **+ New task.** Answer **What kind of work?** (one choice per lane in `dashboard.backlog.new_task`, in the plain words of `lane_labels`); the form shows that lane's fields: an issue number, a task (paste a log into it), a topic, or a new module (id, what it does, depends on), plus an optional **Runs after** picker and a **Stop after** choice listing the lane's phases. `--log` is never offered, because it reads any local path into an agent's input. Cards you added have **Remove**, refused while another card runs after them.
- **A new module.** Its card holds the id, responsibility and dependencies (which must already be in the map). When it starts, the runner (not an agent) writes its row into the capability map in the run's worktree from `dashboard.backlog.module_row` and commits it as the branch's first commit (`module_added`), so the spec steps find it. The row reaches the base branch when you merge the PR; the dashboard never edits the base branch. Two new modules in flight conflict in the map at merge: a one-line fix.
- **Staying current.** Before a start and every 60 seconds the dashboard fast-forwards `project.fork_from` from `origin` (`dashboard.backlog.sync_base`; a branch that cannot fast-forward shows a note and never blocks), re-assesses the map into `runs/queue.json`, and re-checks which pull requests merged. A card that unlocks stays in Backlog until someone moves it.
- **Config.** `dashboard.backlog` in `config.yaml` is the only place a repository's values live: `module_lane` (the lane that runs a map module), `new_task` (lane to form fields), `lane_labels` (lane to the words the page shows; a lane left out shows its own name), `module_row` (column header to cell template for a new module's row) and `sync_base`. `agentflow validate` checks them. A repository with no map shows only added cards.
- **Limits.** `agentflow run` in a terminal is not blocked (a human can override on purpose). Nothing caps parallel spending: each run keeps its own `run_budget_usd`, so the most you can commit is that budget times the cards you start, and phases with `needs_approval: true` still stop for you. Branches that merge cleanly can still break together, so after merging one PR, update the others' branches so CI runs against both. Gates share whatever a repository shares (one database, one port), so they must be safe to run side by side.

### Reading a run

- `agentflow status` lists every run: phase, step, status and spend. `agentflow status <run>` (a unique part of the id is enough) shows one run's params, status, current phase, step and task, spend, branch, worktree and PR, what it is waiting on, the refine round and note, any failure, and every step's status. Gate output, skipped steps with their reasons, and effort changes are in the dashboard's run detail and in `events.jsonl`. `agentflow status --watch` is a live board in the terminal.
- `agentflow agents` lists every agent process running now across runs, with subagents nested. `--watch` refreshes on each event.
- `agentflow dashboard` starts the Board, Workflow, Agents, Metrics and Settings views (Settings: see "Changing behaviour"). The dashboard shows one workflow, with no lane filter or lane badges: lanes still select each run's steps, and the CLI still names them. The Board shows all work in the phase columns; Backlog holds the work nobody has started, so it has no run behind it: capability-map modules and the cards you added (see "Starting work from the dashboard"). The Workflow tab shows each phase's steps with their skill, model and gates, built from `config.yaml`, and badges a step only when one kind of work runs it (`Bug fix only`). A step for two or three lanes has no badge, so a run's drawer is the place to see exactly which steps that run walks. It binds `127.0.0.1` only (port `dashboard.port`, `--port 0` picks a free one) and prints a URL like `http://127.0.0.1:7788/#token=<token>`. Open that exact URL: the token is in the `#token=` fragment, which never reaches the server or its log, and any request without it is rejected. The Reject, Answer, Retry and Resume buttons record the action and start `agentflow resume <run>` in the background, and Pause and Cancel only record it. Approve does the same for a stop inside a phase. A phase approval starts the next phase when that phase has `needs_approval: false`, so approving `plan` starts `build`. When the next phase with work for the lane also needs you, the card rests as `ready` in its column, showing **Continue → <next phase>** and **Mark done**, or only **Mark done** when just `ship` is left. A card shows `agent fixing: <kind>` while the resolver works and `waiting for CI` during the `ci` step. A ready card can also be dragged to the next phase or to Done, and only those two columns accept it. Cards move one phase at a time; there is no jumping ahead or moving back, and Reject redoes a phase. A Done card links its PR. If the push or the PR fails, the card stays out of Done and the reason appears under Needs human.
- **Time and cost.** The dashboard's **Metrics** tab and `agentflow metrics` show where runs spent their time and money, by phase, step, bucket and `model · effort`, for finished and archived runs as well as live ones. The buckets are `agent` (an agent call is open), `gates`, `waiting on a human`, `CI`, `usage-limit hold`, `runner down` (paused, stopped on an error, or a gap longer than `defaults.step_timeout_min` + 15 min with no event, as when a runner was killed) and `runner` (git, commits, prompts). Rework is the cost of calls started for a fix attempt, a retry, a reopened review, a rejection or a refine round. The tab starts at the runs started this month; filter by work and by date (this month, last month, last 7 or 30 days, all time, or a custom from/to). Each phase lists the models and efforts its calls ran on, the run drawer has the same view for one run down to each agent call, and a card shows the run's total time. `agentflow metrics [<run>] [--lane L] [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--all] [--json | --csv]` prints the same data; `--csv` gives one row per run, phase, model and effort. It's all computed from `events.jsonl`, so older runs are covered too. They predate gate timing (`duration_s` on `gate_result`), so their gate time is estimated and marked as such. Codex reports no cost, so its calls show $0. Nothing is ever pruned: Done cards stay on the Board while `dashboard.show_done_days` is `0` (the default), and runs stay in `runs/` until you delete them.
- Everything reads `devtools/agentflow/runs/<run>/events.jsonl` (`state.json` is rebuilt from it and never trusted on its own), so the CLI and the dashboard cannot disagree (the PR body is built from the same events). The agent's notes (`progress.md`) and result files live in its workspace `devtools/agentflow/workspaces/<run>/`. Both folders are git-ignored.

### Changing behaviour (`config.yaml`)

Edit `devtools/agentflow/config.yaml`, then run `agentflow validate`. It checks skills, prompts, conditions, references and settings, and must print no warnings. The agent cannot edit this file during a run.

Or use the dashboard's **Settings** tab. **Form** lists the common settings: run defaults, recovery, effort, context, autonomy, CI, reminders, the dashboard's own settings, each agent's models and turn limit, and each phase's `needs_approval`, `resolve` and model. The comment on each setting's line in the file is shown as its help. **YAML** edits the whole file. **Review and save** shows the diff and what `agentflow validate` says, and saves only when there are no errors. The form changes only the text of the values you changed, so comments, anchors and layout stay as they are. A save made from a page that loaded an older version of the file is refused, so reload the page first. Some settings are protected and are refused from the page, so change them in the file: `gates`, `gate_sets`, `guardrails`, `conditions`, `overlays`, `project`, `phase_approval`, `record_step`, `resolve_step`, `defaults.env`, `context.progress_file`, `hitl.notify`, an agent's command line and permission mode, and a phase's steps and `skip_if`. These decide which commands run, which files reach a prompt or git, and what fences agents in, so the dashboard token cannot become a way to run code. The code is `devtools/agentflow/config_edit.py`.

When a change takes effect: the dashboard uses it at once (a new `dashboard.port` needs a restart), and new runs start with it. A live runner on the shipped config reads the file again before each phase and records `config_reloaded`. If the new file doesn't load or validate, or changes the phases' names or order, the run keeps its config and records `config_reload_skipped` with the reason, and the next `resume` loads the file as it is then. A run started with `--config <other file>` keeps that file as it was loaded. Commit the change like any other edit to the file.

- **Steps:** add or change a step under `phases.<phase>.steps`. Name its `skill` (only a step with `tdd: false` may go without) and tag it with `lanes:` unless it applies to every lane.
- **Skills on Sonnet:** a step on Sonnet names the files its skill needs under `brief:`, and the runner prints them in the prompt at `{brief}`. Sonnet sessions never loaded a skill by name (0 of 96 sessions across the runs up to 2026-11-02; Opus 20 of 25), so naming the skill is not enough. A step whose session already plays a dispatched role takes that role's brief instead of `SKILL.md`: `code-reviewer.md` for reviewers and `implementer-prompt.md` for implementers, because those skills would otherwise dispatch a second agent. `task_briefs:` adds a file for a plan task whose text names a path. `agentflow validate` catches a missing file or a `{brief}` mismatch, and `test_every_sonnet_step_with_a_skill_gets_it_in_its_prompt` catches a new Sonnet step with no brief.
- **Models per phase:** `phases.<phase>.model` is a string, or a map by role with a `default` (`build` uses sonnet for both the implementer and the reviewer). `agents.claude.models` is the fallback. `--model <phase>=<model>` overrides one run.
- **Gates:** add a command under `gates:` and name it in `gate_sets:` (`build`, `verify`). Copy it from `CONSTRAINTS.md`; never weaken it here. Use `when_exists` for gates that need files the repo does not have yet.
- **Conditions:** the `conditions:` block holds the named checks used by `when`, `unless` and `skip_if`. They read files and git, never a model. `agentflow explain` shows how each one decided.
- **Who approves:** `phases.<phase>.needs_approval` is required on every phase, and only a real `false` drops the human. `phases.<phase>.resolve: true` sends that phase's stops to the resolver.
- **Autonomy:** `autonomy:` holds the non-blocking severities, the finding rounds, the stop kinds the resolver takes with their attempts per step, and the switches for recommended answers and reverts. `agentflow validate` refuses a human-only kind such as `budget_exceeded`. `ci:` holds the polling, timeouts and rounds. `resolve_step` and the `ci` step's `fix` are ordinary steps.
- **Budget, push and PR:** `defaults.run_budget_usd` is copied into a run when it starts, so a later config change doesn't reach it. When a feature run's build starts, `defaults.task_budget_usd` (0 turns it off) raises that budget once to fit the plan: the spend so far, plus that amount for each unticked task, plus a quarter of `run_budget_usd` for verify, review and ship (a `budget_scaled` event). It never lowers the budget, and a budget you set with `--budget` stays as you set it. When the run's summed cost crosses it, the runner notifies you and waits for an approval; a plain approve is enough to go on. Automatic fixes spend from the same budget, so it stays a hard stop. `finish` always pushes the run's own branch and opens a draft PR, which the `ci` step marks ready, but first it brings the latest `origin/<project.fork_from>` into the branch (a merge in the run's work tree, never a rebase; nothing is pushed to the target and nothing merges the PR). A conflict goes to the step's `sync` session, and the merged tree must pass the `sync` gates before the runner commits it, so a merge that breaks the build is never pushed; conflicts in security paths, `sync.human_paths` or forbidden paths, and a merge no session can fix, stop for a human with the merge left in the work tree. After the PR is opened `finish` asks GitHub if it merges cleanly and syncs again if it does not (`ci.sync_rounds` bounds both), and each step's own `commit:` says when the runner commits. There are no `defaults.push`, `defaults.pr` or `defaults.commit` settings, and `agentflow validate` refuses them.

### Tool permissions

Headless agents run with `--permission-mode bypassPermissions` (`agents.claude.permission_mode` in `config.yaml`) and `--permission-prompts none`. In that mode `allow` rules have no effect, so an agent no longer stops on a command nobody listed, and nobody adds commands to `.claude/settings.json` for a run. The runner passes no `--allowedTools` or `--disallowedTools`, and `agentflow validate` refuses those flags, or `--dangerously-skip-permissions` and `--permission-mode`, in `base_args`: the mode is set only through `permission_mode`. An unknown mode is also refused. Interactive sessions are unaffected and keep the `allow` / `ask` / `deny` rules in `.claude/settings.json`.

Bypass mode still has limits (Claude Code's permission-modes, permissions and hooks docs):

- **Deny rules hold in every mode.** `git push --force`, `git reset --hard`, `git … --output…`, `uv run git …`, `pnpm exec git …`, `pnpm dlx …`, `Edit(/devtools/agentflow/runs/**)` and the `.env` reads in `.claude/settings.json` stay denied.
- **Ask rules still prompt, and a headless prompt is a denial.** So an agent's own `git push` or `gh pr create` is denied. Only the runner pushes, in `finish`, and only commits it made itself.
- **The runner's `--settings` denies:** an `Edit(./<path>)` rule for each `guardrails.forbidden_paths` entry, plus `RUNNER_ONLY_DENY` in `agents.py` (`Bash(gh *)`). All GitHub calls belong to the runner (`gitops.py`), so `gh api …` or `gh issue …` can't touch the repo from inside a session. Use the `./` form there: `claude` ignores `Edit(/<path>)` from `--settings`.
- **PreToolUse hooks still run and can block**, so `floor_before_push.py` still gates every push.
- **The guards stay the backstop:** forbidden paths, `HEAD` checks before every runner commit, and the commit list checked before the push. A shell command can still write a file, and bypass mode no longer prompts for writes to `.git` or `.claude`.
- Agents get one extra directory with `--add-dir`: their run's workspace `devtools/agentflow/workspaces/<run>/` (inputs copied in by the runner, `progress.md`, the result file). The run's own files under `devtools/agentflow/runs/` (events, state, inbox, lock) are outside it and denied.

What bypass opens: an agent can run any command not covered by a deny or ask rule, such as network calls, `uv run python -c …`, `uvx …`, `pnpm add`, or a dependency change. Review the diff for those, dependency changes most of all. Gates such as `uv run pytest` run code the agent wrote, with your permissions, as before.

`agentflow validate` warns about a gate command part that a headless agent would be denied. In bypass mode that means a part matching an `ask` or `deny` rule. In any other mode, it also means a part that no `allow` rule matches.

The `test_command` a bug run names is a different case. The runner runs it itself, outside Claude Code, so it must still match an `allow` rule from the main checkout's settings with no `ask` or `deny` match (`guards.command_allowed`). Keep those commands in the `allow` list.

To hold runs to the `allow` list again, set `permission_mode: acceptEdits` (or `dontAsk`). Each command an agent needs then has to match an `allow` rule. To allow one during a run, add it to the worktree's `.claude/settings.local.json`. To keep it, add it to `.claude/settings.json` on the main checkout.

Claude Code refuses to start in `bypassPermissions` as root or under `sudo`, and an admin `disableBypassPermissionsMode` setting turns the mode off. Either one makes every agent step fail at once. Run agentflow as your own user, or switch the mode.

### The environment a run gets

Before an agent needs them, a `setup` gate step runs `gate_sets.setup`: `uv sync`, `pnpm install` from the lockfile with no install scripts, and `docker compose -p tivacare-test up -d postgres --wait`. It runs at the start of `build` and `verify`, and before a review fix. No agent is involved, so a failure (Docker not running, say) stops for a human at no cost, and approving runs it again.

`defaults.env` in `config.yaml` goes to every agent session and every gate. It sets `POSTGRES_HOST_PORT` to 55432, which `compose.override.yaml` publishes and `tests/integration/conftest.py` connects to, so a local Postgres on 5432 can't clash. All runs share that one server, because the tests create their own uniquely named databases. It also sets `FORCE_PROMPT_CACHING_5M`, as 5-minute cache writes cost 37.5% less than the default 1-hour ones.

The `verify` gates run the unit and integration suites with coverage, as CI does, then `diff-cover`. A test that can't reach Postgres fails the gate; it is never skipped. Both suites run on pytest-xdist (`-n auto --maxprocesses 4 --dist loadfile`, here and in CI), which takes the integration suite from about 200 s to 85 s. Each worker makes its own databases, and test Alembic runs take a Postgres advisory lock, because migration 0001 creates cluster-wide roles. Implementers run single files without `-n`.

The `verify` step has `gates_first: true`. The runner runs its gates before the session and puts each gate's result line and the last lines of its log in the prompt (`{gate_evidence}`), as evidence for that exact tree. The agent reports from it instead of running the suites again, and runs only the plan's own checks that no gate covers. A failure there starts the session in its fix loop. Afterwards the gates run again only if the session changed the tree (`gates_reused` in the events when it didn't).

The `verify` step's last gate, `web_e2e`, runs the Playwright suite against the full Compose stack, as CI's e2e job does, through `python3 devtools/e2e_stack.py` (implementers call the same script, with a path, for `webadmin/e2e/` tasks). Only that step names it (`gates: [verify, web_e2e]`). It stays out of `gate_sets.verify`, which the bug lane's fix loop, `fix_findings`, `sync` and the CI fix also run: rebuilding the stack on every attempt there would add minutes and push sessions past the 5-minute prompt cache, and CI's own e2e job checks the PR. An agent can't set that stack up itself: the `.env` deny rules block any command that names the file, and the stack binds fixed host ports (80, 443, 9000). The script is the runner's, so permission rules don't apply to it. It takes a machine-wide lock (`~/.cache/tivacare/e2e.lock`, waiting up to 15 min and then naming the holder), so runs take turns. It removes any stack a killed run left under the `tivacare-e2e` project, and refuses to start when another stack holds one of the ports, naming it (it never stops a stack it didn't start). It configures the stack from `.env.example` alone, as CI does (`docker compose --env-file .env.example`): your own `.env` is never read or changed, and the keys `.env.example` sets are dropped from the inherited environment, so an exported `APP_ENV` or `DATABASE_URL` can't override them. It publishes the stack's Postgres on 55433, off the shared test server. Then it builds, waits for `/api/health`, seeds, runs Playwright and CI's SC 12 and SC 8 checks, and always takes the stack down. Exit 2 means the environment (Docker down, a port taken, the lock held, the stack never healthy) rather than the code. If you have a dev stack of your own up, stop it before a run reaches `verify`.

### Commits, pushes and PRs

- `devtools/agentflow/config.yaml` is the standing permission for these, inside `agentflow` runs only. A normal Claude Code session still commits and pushes only when asked.
- Every lane works in its own worktree under `project.worktrees_dir`, on `flow/<lane>/<slug>` forked from `project.fork_from`, so a run never touches the main checkout. The runner commits with the `project.commit_trailer` line (per task in `build`).
- Every lane ends with the `ship` phase, which brings in the latest `project.fork_from`, pushes the branch and opens a draft PR to `project.fork_from`, whose body ends with the Claude Code attribution line. `design` first makes its one commit (`Decide <topic> in agentic RAG idea doc`). `finish` refuses to push `project.base_branch` itself. Nothing merges a PR, and a Done card means the draft PR is open, not that it merged.
- A `--to define` run that you approve from the CLI stops before `ship`: it only commits, on the run's worktree branch, so you review and merge or push those commits yourself. Approved on the dashboard, it rests as `ready`, and **Mark done** opens its PR.

### Next step and the module queue

- `agentflow next` answers "what now?" in three lines (lane, stage, next skill) from the repo as it stands, the same job as `/next-step`.
- `agentflow archive <run>` files a finished run away as old data: its card moves to Done with the badge `archived`. Only the card changes. The run keeps its status and its branch, so a spec-only run still does not count as starting its module, and a run that is not finished is refused.
- `agentflow queue` starts modules in `docs/specs/CAPABILITY-MAP.md` order (in the lane `dashboard.backlog.module_lane` names; without it the queue refuses to start) as their dependencies merge, at most `--max-parallel` (default 2) runs live at once. `--once` ticks one time and exits (for cron); `--interval` sets the seconds between ticks. The code is `devtools/agentflow/module_queue.py`. A merged PR (`branch_merged`) is what marks a module done and unblocks the next. A module marked done before its plan is finished doesn't move again by itself, and the queue logs that it needs a human.
