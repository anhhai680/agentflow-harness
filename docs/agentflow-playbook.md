# Agentflow playbook

How to run `agentflow` in a repository with Claude Code or Codex. It is a task guide: follow it top to bottom the first time, then use the cheat sheet in section 10.

This repo is the reference setup: its own pack is `.agentflow/config.yaml`. The design is in `docs/agentflow-harness-proposal.md` and the build records are in `docs/plans/`. This playbook does not repeat them. It tells you what to type and what to check.

## 1. What agentflow does for you

`agentflow` walks one main workflow without you typing each skill: `define → plan → build → verify → review → ship`.

- Each **phase** has several **steps**, and each step maps to one skill.
- A **lane** (`feature`, `bug`, `eval`, `design`) names the kind of work and selects which steps run. A phase with no step for the lane is skipped with no approval. The CLI uses lane names. The dashboard shows one workflow and asks "What kind of work?" in plain words instead (`dashboard.backlog.lane_labels`).

- Each agent step is **one headless call** (`claude -p …` or `codex exec …`) with one skill and one prompt. It writes a result file and exits.
- **Shell gates** (lint, types, tests, the floor guard) run after the steps that name them. A failing gate resumes the same session with the output, up to `defaults.fix_attempts` times.
- **You approve `define` and `plan`.** Each phase's `needs_approval` in `config.yaml` says so. Reject with a note and the phase re-runs as a refine round. `build`, `verify`, `review` and `ship` move on by themselves.
- **The agent handles its own problems after `plan`.** A stop goes to a fresh resolver session first, blocking review or verify findings get an automatic fix plan and fix round, and a CI failure gets a fix and a push. You hear about it only when those run out.
- **A usage limit or an outage is a hold, not a stop.** The run shows as `held`, waits, and carries on by itself (section 8).
- The **runner is the only thing that commits and pushes**. Agents never do. `feature`, `bug` and `eval` runs end with a PR marked ready for review once CI passes. Nothing merges it but you.
- All state is an event log under `<state dir>/runs/<run>/` (the state dir is outside the repository by default; see `paths.state_dir` in section 14). The CLI, the dashboard and the PR body read the same events.

You stay in charge of three things: answering the questions in `define` and `plan`, approving those two phases, and merging the PR.

## 2. Prerequisites

| Need | Check |
|---|---|
| Python 3.12+ and `uv` | `uv --version` (the installed tool and `bin/agentflow` both run on `uv`; PyYAML comes from the package or the script header) |
| `git` with an `origin` remote and the fork branch (`main` here) | `git remote -v`, `git branch --list main` |
| GitHub CLI, signed in (needed by `ship`, `--issue`, and `queue`) | `gh auth status` |
| Claude Code (the default agent) | `claude --version`, and run `claude` once interactively so it is signed in |
| Codex CLI (optional second agent) | `codex --version`, and `codex login` once |
| The repo's skills at `.claude/skills/<name>/SKILL.md` | `ls .claude/skills` |
| A clean working tree on the fork branch (design runs require it) | `git status`, `git branch --show-current` |

Agent runs spend real money, and a run has two budgets. `defaults.run_budget_usd` (25 here) is the soft one: when the run's summed cost crosses it, the runner sends one notification (`budget_soft_reached`) and the run goes on. `defaults.run_budget_hard_usd` (65 by default) is the hard one: crossing it stops the run with `budget_exceeded` for a human's approval. A plain `approve` of that stop sets the hard budget to the spend so far plus one soft budget, and `--budget <usd>` sets it exactly. A feature run's soft budget grows to fit its plan when `build` starts (`defaults.task_budget_usd` per open task), while the hard one never does: if the estimate is above it, you get one notification and the run goes on. A run started before the soft budget existed keeps its one budget, which stops it. The runner checks before each agent call, so a call already running can go past either budget. Claude's cost is what Claude reports, or an estimate from `agents.claude.prices` for a session that ended without a report (a context rollover, a timeout). Codex steps are recorded as 0, so the budget does not count them. The hard budget now defaults to 65, and `validate` refuses a hard budget below the soft one: if you set `run_budget_usd` above 65 (in the pack or the user file), also set `run_budget_hard_usd`, because it must be at least the soft budget.

## 3. One-time setup

Install the engine once as a tool, from a checkout of this repository. This puts an `agentflow` command on your PATH, and it runs from any directory:

```bash
uv tool install --from /path/to/agentflow-harness agentflow     # first install
uv tool install --reinstall --from /path/to/agentflow-harness agentflow   # after pulling engine changes
```

A `git pull` of the checkout does not change the installed tool, and a plain `uv tool install` on an existing install does nothing: after pulling, always reinstall with `--reinstall`. The guided update flow (an agent can run it for you) is "Updating an existing install" in `SETUP.md`.

`pyproject.toml` lists the files under `agentflow/` as `cache-keys`, so uv rebuilds its cached wheel when one changes. If an install still serves stale code, `--reinstall` is the fallback. `bin/agentflow` stays for working on the engine itself: it runs `agentflow/flow.py` straight from the checkout, so an edit takes effect with no reinstall.

Then run these from the repo root of the project you drive.

```bash
# 1. The config must be valid. It must print "0 warnings".
agentflow validate

# 2. Fast sanity check of everything the runner can see, with no agent calls.
agentflow next

# 3. Optional but recommended: run the tests once (25-60 minutes; a loaded machine is slower).
#    --no-project keeps uv from picking up any project file: the engine needs only pyyaml and pytest.
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q
```

Then check the three files that decide what agents may do:

- `.agentflow/config.yaml` is the one file with all behaviour: models per phase, gates, conditions, budget, push and PR. **Agents cannot edit it during a run.** Run `agentflow validate` after every edit. It is the repository's pack (section 14), read from the main checkout even when you run `agentflow` inside a run's worktree.
- `.claude/settings.json` (shared) and `.claude/settings.local.json` (yours, git-ignored) hold the tool rules for Claude. The runner passes no `allow` rules. It passes `--settings` deny rules of its own (the pack's forbidden paths, including `.agentflow/**`, and the state dir's runs root), and headless agents run with `--permission-prompts none`, so any command that would prompt is denied. If a step needs a command, add an `allow` rule for exactly that command.
- The `gates:` and `gate_sets:` in `config.yaml` are the repository's quality bar. Never weaken a gate to get a run through.

A denied command does not stop a run by itself. The runner records a `permission_denied` event on the step, and the agent carries on without the command, usually ending the step as `blocked`. That is the system working. Decide, add the narrow rule, then `agentflow approve <run> <step-key>` to resume the step's session, or `agentflow retry <run> <step-key>` to start it fresh.

## 4. Pick the lane

| You want to… | Lane | Command | Ends with |
|---|---|---|---|
| Build a module end to end | `feature` | `agentflow run feature --module crm` | draft PR |
| Write or amend a spec only | `feature`, stopped after define | `agentflow run feature --module crm --to define` | commits on the run's branch, no PR |
| Fix a bug | `bug` | `agentflow run bug --issue 42` (or `--log PATH`, or `--task "…"`) | draft PR |
| Change a prompt, model, chunking or retrieval | `eval` | `agentflow run eval --task "…"` | draft PR (eval gate must pass) |
| Decide a design question in the idea doc | `design` | `agentflow run design --topic "Clarify thresholds"` | one commit on its own branch, draft PR |

Not sure which? `agentflow next` prints the lane, the stage and the next skill for the repo as it stands.

Every command starts with `agentflow`. Put it on your PATH once, from the repo root (`~/.local/bin` must be on your PATH):

```bash
ln -s "$PWD/bin/agentflow" ~/.local/bin/agentflow
```

## 5. The first run, step by step

Use a small module or a spec-only run for your first time.

**Step 1. Ask what would run, with no agent calls.**

```bash
agentflow explain feature --module crm
```

Each phase and step shows `✓ run`, `– skip (reason)`, `? ask (reason)` or `⏸ <phase>_approved`. A skip reason names the condition that decided it (for example, the spec is already approved). If it says `? ask`, a condition could not be decided from the files. The run will ask you.

**Step 2. Preview the commands.**

```bash
agentflow run feature --module crm --dry-run
```

It prints the agent command per phase and the gate commands, and creates nothing: no run folder, no worktree, no agent call. Check the models and the gates.

**Step 3. Start the run.**

```bash
agentflow run feature --module crm
```

It creates a worktree under the state dir's `worktrees/<run>` (`project.worktrees_dir` in the pack moves it) on `flow/feature/<slug>`, forked from `main`, and starts at the first phase that is not skipped. It stops at the first question or approval. The run id looks like `20261030-0915-feature-crm`.

Useful options: `--from plan --to build` (start and stop at chosen phases; the `--to` phase still raises its approval), `--model plan=sonnet` (one phase's model, this run only; it replaces that phase's whole role map), `--detach` (run in the background, logging to `<state dir>/runs/<run>/runner.log`).

A foreground run outlives its terminal: if the window closes, the runner carries on and writes to the same `runner.log`.

**Step 4. Answer, review, approve.** Every stop lands in the inbox:

```bash
agentflow inbox                       # every run waiting on you, with its questions
agentflow status                      # all runs: phase, step, status, spend
```

For questions, use the exact id the inbox shows (ids are case-sensitive, and agents normally write `q1`). Repeat `--set` per question. The run waits until every question has an answer.

```bash
agentflow answer <run> --set q1="Use the per-process token cache"
```

With no `--set`, `agentflow answer <run>` asks each unanswered question in the terminal. You can also fill the `Answer:` lines in the file `agentflow inbox` names, then run `agentflow answer <run>` to record them: editing the file does not continue the run by itself. A `spec_gap` answer is written into the spec as its own `Decide …` commit, and `--record` does the same for any other answer.

For a phase approval, read the handoff and the diff the inbox shows, then:

```bash
agentflow approve <run> plan          # next phase starts in a fresh session
agentflow reject  <run> plan --note "Split task 3 into two tasks"   # refine round; note is required
```

Rejecting a stop inside one plan task (`build/build/task-2/implement`, `…/review` or `…/commit`) sends only that task back for a refine round; the other tasks keep their commits and start no session. Rejecting the phase still refines every task. In any refine round of `build`, a task whose implementer answers `unchanged` and leaves the tree alone gets no review and no gate run (while the run is in `build`, its card shows `– review task N (unchanged)`).

`approve`, `reject`, `answer` and `retry` record the action and then **continue the run in the foreground**. Add `--detach` to continue in the background, or `--no-continue` to only record. Wherever a command takes `<run>`, any part of the id that matches one run is enough.

On the dashboard, approving `plan` starts `build`, because `build` needs no human. Approving `define` doesn't start `plan`: when the next phase also needs you, the card rests as **ready** in its column, and you choose:

- **Continue → `<phase>`**, or drag the card to that column: the next phase starts. Same as `agentflow resume <run>`.
- **Mark done**, or drag the card to Done: the remaining phases are skipped, `ship` pushes the branch and opens a **draft PR**, and the card moves to Done with a link to the PR. Same as `agentflow resume <run> --mark-done`. The dialog names the phases that won't run, and the PR lists them too.

Cards move one phase at a time. To redo a phase, Reject it before approving. If the push or the PR fails, the card stays out of Done and the reason appears under Needs human; fix it and approve the stop to try again, which never opens a second PR.

**Step 5. Repeat until the last phase.** `build` commits per task (each task: implement, gates, review, one commit). `ship` pushes the branch and opens a **draft PR** to `main`. Review that PR on GitHub as you would any other. Done on the board means this draft PR is open; merging it is still yours.

## 6. Running with Claude and with Codex

### Claude (default)

Nothing to set up beyond section 2. `agents.claude` in `config.yaml` controls it. The models are set per phase (`phases.<phase>.model`), with `agents.claude.models` as the fallback for calls outside a phase's own steps, such as recording a decision. A role map picks different models inside one phase, and needs a `default`:

```yaml
phases:
  build:
    model: {default: sonnet, review: opus, plan: opus}
```

| Phase | Model |
|---|---|
| `define` | opus; sonnet for `apply`, which files answers you already gave |
| `plan` | opus |
| `build` | sonnet for the implementer; opus for the per-task reviewer and the eval lane's `change` step |
| `verify` | sonnet |
| `review` | opus for the reviews; sonnet for `fix_findings` |
| `ship` | none, it calls no agent |

Every step's first session starts at `medium` effort. The agent may ask to restart at `low` or `high` before it does any work. The runner records the level and passes `--effort` on every call, including resumes. Project skills load headlessly from `.claude/skills/`. Tool rules come from `.claude/settings*.json`.

### Codex

Codex support is an **adapter only, disabled by default, and not yet exercised against a real Codex session** (the tests use a fake agent). Treat the first Codex run as a pilot on a throwaway task.

1. **Create a Codex profile** named `agentflow` in `~/.codex/config.toml`. The sandbox and approval policy live there, not in agentflow. An example (check the key names against `codex exec --help` for your version):

   ```toml
   [profiles.agentflow]
   sandbox_mode = "workspace-write"
   approval_policy = "never"
   ```

2. **Enable the agent:** set `agents.codex.enabled: true` in `.agentflow/config.yaml`. The rest of the block is ready (`profile: agentflow`, the models, the effort argument). From then on every `agentflow run` needs `codex` on your PATH, even a run that stays on Claude.

3. **Move one phase to Codex.** Start with a phase that is easy to review, such as `review` or `build` on a small module. Add `agent` and replace the phase's `model`, and leave its `steps` as they are:

   ```yaml
   phases:
     review:
       agent: codex
       model: gpt-5-codex   # was {default: opus, build: sonnet}
       steps:
         # unchanged
   ```

   **Always change `model` together with `agent`.** A phase's model wins over `agents.codex.models`, and `agentflow validate` checks only that the phase's agent is enabled. A Claude role map left in place passes `validate`, then sends `-m opus` to Codex. For the same reason, `defaults.agent: codex` puts every phase on Codex only if you also replace every phase's `model`.

4. **Validate and preview, then run.**

   ```bash
   agentflow validate
   agentflow run feature --module crm --from review --dry-run     # the argv shows codex exec … -m gpt-5-codex -c model_reasoning_effort=…
   ```

What differs when Codex is the agent:

| Topic | Claude | Codex |
|---|---|---|
| Skills | loaded natively from `.claude/skills/` | the prompt says to read `.claude/skills/<skill>/SKILL.md`, and `AGENTS.md` at the repo root carries the rules |
| Tool permissions | `.claude/settings*.json` allow, ask, deny | the `agentflow` profile's sandbox. `.claude/settings.json` does **not** apply to Codex |
| Effort | `--effort <level>` | `-c model_reasoning_effort=<level>` |
| Cost in `status` and the budget | recorded, or estimated from `agents.claude.prices` | recorded as 0, so neither budget (`run_budget_usd`, `run_budget_hard_usd`) counts a Codex-heavy run |
| Resume | `--resume <session>` | `codex exec resume <id>`, with the writable folders passed as `-c sandbox_workspace_write.writable_roots=…` (unverified end to end) |
| Security review profile | `--agent security-auditor` | not used |
| Usage limits | waits for the reset time | not detected: a failed call counts as an outage, and it retries until `recovery.give_up_h` |
| Context rollover | a fresh session at `context.rollover_pct`, or once a session has compacted `context.max_compactions` times | none |
| `agentflow attach` | opens the step's session | refused |

The guards still hold for any agent: forbidden paths, `HEAD` moved by an agent fails the step, only the runner commits. Because Codex ignores `.claude/settings.json`, check the `agentflow` profile yourself: it must not allow network or writes outside the work tree that you would not allow for Claude.

## 7. Lane recipes

### Feature (`feature`)

Phases: `define`, `plan`, `build`, `verify`, `review`, `ship`. Work that already exists is skipped with a reason (an existing spec skips the idea questions and the draft; an existing plan with open tasks skips `plan`; a plan with every task ticked skips `build`). Expect approvals at `define` and `plan` only. `define` runs `grilling` (only without a spec), `spec-driven-development`, `api-and-interface-design` and the open-questions grilling, each when it is needed. `plan` runs `writing-plans`, which also uses `codebase-design` for a package that doesn't exist yet. That design stays in the plan: a public interface the approved spec doesn't have stops the run with a `spec_gap` question. `build` uses `subagent-driven-development` above 3 tasks and `executing-plans` for 3 or fewer (one shared session, no per-task review). On a security path (token validation, `customer_code`, `visibility = public` filtering, attachments, redaction) the plan's approval page offers `/doubt-driven-development`: run it yourself before you approve, or approve to skip. The runner never runs it. The same path adds `security-and-hardening` to every step with the `plan` or `review` role, and a `security_review` step to the `review` phase.

### Spec only

`agentflow run feature --module crm --to define` runs the `define` phase and ends after you approve it. Approved from the CLI, it only commits, on the run's worktree branch, and never pushes: you review the commits, then merge or push them yourself. Approved on the dashboard, it rests as ready, and **Mark done** opens its draft PR. It doesn't count as starting the module in `agentflow queue`, and the Board keeps it in the `define` column with the badge `stopped after define`.

### Bug (`bug`)

`define` (the `debug` step) → `build` (`regression_test`, then `fix`) → `verify` → `review` → `ship`. The regression test must **fail first**: if the agent's test command passes before the fix, the run stops with `red_test_passes`. After the fix, the same command must pass in addition to the gates. The command is the agent's text, so agentflow refuses shell syntax (`;`, `|`, `&`, `$`, redirects), runs it as an argument list without a shell, and runs it only if an `allow` rule in the main checkout's `.claude/settings*.json` covers it. If it stops with `blocked`, add the narrow rule or change the command, then approve.

### Eval (`eval`)

`build` (the `change` step) → `verify` (the eval gate) → `review` → `ship`. For changes to prompts, models, chunking, embeddings or retrieval. The eval gate must pass. A regression stops the run with `eval_regression`. `eval/baseline.json` is a forbidden path: only a human changes it.

The gate is not only for this lane. A `feature` or `bug` run whose diff touches `overlays.eval.when_paths` runs it in `verify` too, and any run needing it runs it again after `fix_findings`, so a review fix cannot skip it.

### Design (`design`)

Decides open questions in the idea doc. Like every lane it works in its own worktree on `flow/design/<slug>`, so the main checkout can be on any branch and hold any work. Flow: `define` writes questions, you answer, it edits the doc (a Decisions Log row, the Open Questions removal, and an ADR index entry when an ADR is added; checks enforce them), you approve, and `ship` makes one commit `Decide <topic> in agentic RAG idea doc`, pushes the branch and opens a draft PR.

## 8. Operating a run

| Goal | Command |
|---|---|
| All runs, phase, step, status, spend | `agentflow status` (live: `agentflow status --watch`) |
| One run in detail | `agentflow status <run>` |
| Agent processes running now (model, effort, context, spend, idle) | `agentflow agents` (live: `agentflow agents --watch`) |
| Board, workflow and agent views in a browser | `agentflow dashboard` (prints a `http://127.0.0.1:<port>/#token=…` URL; port 7788 by default, `--port 0` picks a free one) |
| Everything waiting on you | `agentflow inbox` |
| Continue a paused, `stalled`, `error` or `ready` run | `agentflow resume <run>` (a stop waiting on you stays until you approve, answer or reject it) |
| Finish a `ready` run here and open its draft PR | `agentflow resume <run> --mark-done` |
| Run a step again in a fresh session | `agentflow retry <run> <step-key>` (with no key: the run's failed step, which only a `guard_violation` leaves) |
| Pause (the running step finishes first) | `agentflow pause <run>` |
| End a run and stop its agents (cannot be undone) | `agentflow cancel <run>` |
| Open the waiting step's Claude session by hand (no runner may be live) | `agentflow attach <run>` (then `agentflow resume <run>`) |

**Stops you will meet** and what to do:

| Stop | Meaning | Do |
|---|---|---|
| `<phase>_approved` | `define` or `plan` finished, or findings in a later phase outlasted the fix rounds | read the handoff, diff and findings, then `approve` or `reject --note`. Approving a later phase ships its open findings |
| questions in the inbox | the agent cannot settle something from the docs (`needs_human`) and the runner did not take its recommended answers (a `boundary` question, no single recommended option, a question asked again, or `answer_recommended: false`), or the runner cannot decide a condition (`condition`, a yes or no under the id `answer`) | answer, then the run continues |
| `fix_attempts_exhausted`, `blocked`, `step_timeout`, `max_rollovers` | the runner gave up on the step | fix the cause, then `approve <run> <step-key>`: the step's session resumes and is still verified and committed. `retry` starts it fresh |
| `agent_unavailable` | calls kept dying after reaching the model, or an outage outlasted `recovery.give_up_h` | check the step's transcript, then `approve <run> <step-key>`: the step's session resumes |
| `budget_exceeded` | the summed cost crossed the hard budget, `run_budget_hard_usd` (the soft `run_budget_usd` only notifies) | `approve <run> <step-key>` allows the spend so far plus one soft budget; `--budget <usd>` sets the hard budget to a number above the spend so far |
| `guard_violation` | a step touched a forbidden path twice (the runner puts the first back), moved `HEAD` or used a user-only skill | undo the change, then `approve <run> <step-key>`: the runner checks the tree again and restarts the step in a fresh session. Approving does not let the change through |
| `review_unresolved` | blocking review findings remain after the fix round and the resolver | fix by hand if you want, then approve: the reviewer looks once more, then the task commits, with any open finding in the commit body. Only your approval can do that |
| `ci_failed` | CI still fails after `ci.fix_rounds`, reports no checks, or never settles | read the logs in the inbox, fix the cause, then `approve <run> ship/ci` for fresh rounds |
| `red_test_passes`, `eval_regression`, `gate_failed` | a verification step failed | fix the cause, then approve to re-check |
| `max_rounds` (`define/grill_open_questions`) | the spec still has open questions after 2 grilling rounds | `approve` goes on as it is; `reject --note` sends the phase back |
| `dirty_tree` (at `ship`) | uncommitted changes in the worktree | commit or discard them, then approve |

**Holds are not stops.** A usage limit or an outage needs nothing from you. The run shows as `held`, keeps its queue slot, and notifies you once. `agentflow status <run>` prints the reason and the time, and the Board's badge reads like `held until 14:30 (usage limit)`.

- A usage limit waits for its reset time, with no cap.
- An outage retries with a backoff of up to 15 minutes, for `recovery.give_up_h` (24) hours.
- A call that reached the model and still died is retried, but the third such failure (`recovery.failed_attempts`) stops the step, because those calls cost money.

The last two end in an `agent_unavailable` stop. `pause` and `cancel` still work during a hold. If the runner itself dies during a hold, nothing restarts it: run `agentflow resume <run>`.

**Most questions never reach you.** A question's `kind` is `spec_gap`, `ambiguity`, `conflict`, `review_dispute`, `boundary` or `other`, and the agent marks one option of each as recommended. In every phase the runner takes those options itself (`autonomy.answer_recommended`), records them as answered `by: runner`, notifies you with one line, files a `spec_gap` answer in the spec as its own `Decide …` commit, and lists each decision under "Decisions the runner took" in the pull request, so you review them there. A `boundary` question is one the repository's docs reserve for a human (an "ask first" or "never" rule): it is in `autonomy.human_question_kinds`, so it always stops for you, parked or not, and your answer goes in the task's commit body, as an `ambiguity` or `review_dispute` answer does. A question with no single recommended option, or one the step asks again after the runner answered it, also comes to you.

**Parked questions are not stops either.** When a plan task asks a question that later tasks can go on without, the run parks that task and carries on with the tasks that do not depend on it. It stops only when no runnable task remains. `agentflow inbox` lists the task as `parked`, and `agentflow answer <run> --key <step-key>` answers it.

A resolver session may also record what it found. Its result can carry `facts`, short statements that hold for the whole run (for example, a gate that fails for a reason outside the task). The runner keeps each new one as a `run_fact` event, and every later agent session of the run sees them under "What this run already found" in its prompt, so it checks them instead of diagnosing the same thing again.

Any step's result may carry `code_map`: up to 8 one-line entries naming existing code the step had to search for (`path:line`, signature, use). The runner keeps them as `code_noted` events (the newest 40, deduplicated), and every later session of the run sees them under "Code this run has mapped" in its prompt, so it opens that code at the line given instead of searching for it again.

The runner fixes what a review finds by itself: `plan_fixes` makes the plan, `fix_findings` makes the change and runs its gates, then fresh review sessions check the branch again. If you stop at `review_approved` because findings outlasted those rounds, reject with a note to send it round again.

The dashboard's Workflow tab shows every phase with its steps, skills, model and gates, from `config.yaml`, with a badge only on a step that one kind of work runs. To see exactly which steps a run walks, open its drawer. The Board's Backlog column is not a phase: it holds capability-map modules the queue hasn't started, so they have no run yet. Dragging only works on ready cards.

Approving a stop **never** lets a failing gate, a passing red test or a forbidden change through. It resumes the step and checks again.

Notifications: a terminal bell and a macOS notification fire when a run needs you, and once when a run goes on hold. A run waiting more than `hitl.remind_after_h` (4) hours is re-notified while `agentflow queue`, `agentflow dashboard` or a `--watch` view is running. Waiting never times out into a decision.

## 9. Unattended runs and the module queue

```bash
agentflow queue --max-parallel 2          # loop, every --interval seconds (30): start modules from the capability map as dependencies merge
agentflow queue --once                    # one tick, for cron
```

A module is done when its plan is fully checked and its PR is merged. The queue starts ready modules in `docs/specs/CAPABILITY-MAP.md` order, never above `--max-parallel`. A run that is waiting for you, paused or held keeps its slot but does not block the others. It prints `queue: <module> blocked — <reason>` once per change: an unauthenticated `gh`, a failed, cancelled or crashed run, a start that failed, an unknown dependency, a dependency cycle. Failed and crashed runs are never restarted automatically. Only one `agentflow queue` runs at a time, and its start output goes to `<state dir>/runs/queue.log`.

## 10. Cheat sheet

```bash
agentflow init [--starter NAME] [--baseline]        # write a sparse pack for this repository (section 13)
agentflow doctor                                    # agent CLIs and logins, gh, origin, Docker, state dir, gate tools,
                                                    #   and the inherited variables a run drops (names only)
agentflow validate                                  # after every config edit; 0 warnings
agentflow config show                               # the pack file as written
agentflow config show --effective [PREFIX]          # every value after the layers merge, with its layer
                                                    #   (engine, project or user); PREFIX e.g. defaults, agents.claude
agentflow next                                      # what now?
agentflow explain <lane> --module X                 # what would run, no agent calls
agentflow run <lane> … --dry-run                    # the commands, creates nothing
agentflow run <lane> --module X [--from P] [--to P] [--model P=m] [--detach]
agentflow inbox | status | agents                   # what is waiting, running, spent
agentflow answer <run> --set q1="…"                 # ids as the inbox shows them; --key for a parked step
agentflow approve <run> <phase|step-key> [--budget N]   # soft budget notifies, hard budget stops; --budget sets the hard one
agentflow reject <run> <phase|step-key> --note "…"     # a stop inside a plan task refines only that task
                                                       #   (`reject <run> <task-stop-key>`); a batched task's stop is rejected phase-level
agentflow retry <run> <step-key>                    # a fresh session
agentflow pause | resume | cancel <run>
agentflow resume <run> --mark-done                  # a ready run: skip to ship, open the draft PR
agentflow dashboard                                 # token is in the URL fragment; keep it private
agentflow queue --max-parallel 2
agentflow queue --include-specs                     # a pack with work_source specs: queue every spec file's module
agentflow backlog done <module> [--by NAME]         # mark a module finished by hand (refused while it has a live run)
agentflow backlog undone <module> [--by NAME]       # remove that hand mark; with none, ignore the origin evidence that
                                                    #   says it is done (a merged module stays done)
```

## 11. Safety model in five lines

1. Agents get one writable folder besides the work tree: `<state dir>/workspaces/<run>/`. The run's events, state, inbox and lock live in `<state dir>/runs/`, outside it, and the runner denies agents any edit there.
2. The runner refuses a step that touches a path in the pack's `guardrails.forbidden_paths` (such as `.claude/**` or the golden test files) or the pack itself (`.agentflow/**`, always), that moves `HEAD`, or that uses a user-only skill. It re-checks before every commit and before the push. A plan task that names such a path (a `Create:` or `Modify:` line) stops `build` before its first task and shows in `explain`: a human makes that change, or the plan drops it.
3. Only the runner pushes, only commits it made, never with `--force`. It never writes `Constraints-Approved-By`.
4. The dashboard listens on `127.0.0.1` only and every request needs the token. Do not paste the dashboard URL into chat or tickets.
5. **Residual risk:** gates such as `uv run pytest` execute code from the work tree, which the agent wrote, with your permissions. The guards catch what that code changes in git, not everything it could do on your machine. Review the diff at every approval, and for risky work run agentflow in a disposable environment.

## 12. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| A run fails on a missing tool or a login, or you are setting up a new machine | `agentflow doctor` checks, in order, each enabled agent's CLI and login (`claude auth status`, `codex login status`), `gh auth status` (needs the network), the `origin` remote, Docker (only when a gate runs `docker`), that the state dir is writable, the variables a run does not inherit (`inherited environment`, a warning that names them), and that each gate's programs are on `PATH` (`defaults.env.PATH` when set). It prints `ok`, `warn` or `fail` with a fix for each, and exits 1 on any `fail`. A gate program that `gate_sets.setup` may install is a warning, not a failure. A broken user file is reported as a `user config` failure and the checks go on without it |
| A gate or an agent step fails only inside a run, and passes in your own shell | a variable your shell or IDE loaded from `.env` may have been dropped from the run, or one that leaked in may differ from the pack's. `agentflow doctor` has an `inherited environment` check that names the variables a run drops and the file they match (never their values). To keep one, set it in `defaults.env`. `defaults.env_inherit` (playbook section 14) says what is dropped |
| `validate` or `doctor` fails on a key the pack uses right after you pulled the harness (for example `work_source.path: missing`) | the installed engine is older than the checkout. Run `uv tool install --reinstall --from /path/to/agentflow-harness agentflow` (section 3), then `validate` again |
| `agentflow validate` prints warnings or errors | the message names the YAML path (`phases.build.model`, `phases.define.steps[4].when`). Fix that line and re-run |
| `run` says the agent command is not found | the preflight checks the `command` of each enabled agent. Install it or fix `agents.<name>.command` |
| A run shows `held` | a usage limit or an outage. It carries on by itself (section 8); `pause` or `cancel` if you don't want it to |
| A run shows `stalled` | the runner died (Ctrl-C, a crash, a reboot). A closed terminal does not cause this. `agentflow resume <run>` first waits for any agent that outlived its runner, up to the step timeout, then picks the step up from its recorded session |
| A run shows `error` | the runner stopped on an unexpected exception (event `runner_error`). Fix the cause, then `agentflow resume <run>` |
| Another runner is already on this run | the run lock refuses a second runner. Wait, or `agentflow status <run>` to see it |
| `approve` says "not awaiting approval" | someone (CLI or dashboard) already approved it, or the target is not the exact key. The message names the key the run waits on, such as `build/doubt_offer` |
| A step ends `blocked` and its events show `permission_denied` | a command is not covered by an `allow` rule. Add the narrowest rule to `.claude/settings.json` (or your `settings.local.json`), then `agentflow approve <run> <step-key>` |
| `ship` stops with `dirty_tree` | uncommitted files in the worktree: commit or discard, then approve |
| The queue prints `blocked — could not tell whether … is merged` | `gh` failed. Run `gh auth status` |
| Codex steps show 0 cost | expected. Codex cost is not reported; set a stricter `run_budget_hard_usd` or watch the Codex dashboard |
| A run is stuck and nothing helps | `agentflow status <run>` for the step, the run's `events.jsonl` (read-only) for the timeline, `agentflow attach <run>` to look at the session. Last resort: `agentflow cancel <run>` and start again |

## 13. Using agentflow in another repository

The engine (`agentflow/`) names no project: everything a repository decides is in its pack, the `.agentflow/` directory at the root of its main checkout (section 14). `agentflow init` writes a sparse pack, about 15 lines, and everything it doesn't write comes from the engine defaults and a starter (`python-uv`, `node-pnpm`, `go` or `docs-only`).

1. **Install the engine** as a tool, once per machine (section 3): `uv tool install --from /path/to/agentflow-harness agentflow`.
2. **Run `agentflow init [--starter NAME] [--baseline]`** from the repository, on any branch. It reads the base branch through git, never the working tree (unless the repository has no commit yet), and runs nothing from the repository unless you pass `--baseline`. It:
   - finds the base branch from `origin`, picks the starter from `pyproject.toml`, `package.json` or `go.mod` (`--starter` overrides it), and proposes gates: from the CI workflow steps first (`.github/workflows/*.yml`), then `package.json` scripts, `pyproject.toml` tool sections and `Makefile` targets, else the starter's. A step it can't run the way CI does (an action, an install, a matrix value) is skipped with a note.
   - finds `docs/plans`, ADR and spec folders, the repository's own skills and its `CLAUDE.md`/`AGENTS.md`;
   - when it found a top-level `specs/`, `docs/specs`, `docs/rfcs` or `docs/proposals` folder, also writes a fixed `work_source: {kind: specs}` block and a `dashboard.backlog` block (`module_lane: feature`, plus the New task form's fields), so the Backlog lists one module per spec file in `project.docs.specs` and a new feature card writes a new spec. When most files in that folder are named `PREFIX-<module>.md` (a short letters-and-digits prefix, at least two files, more than the plain `<module>.md` ones), it also writes `project.docs.spec_file: "PREFIX-{module}.md"`, so those files are modules and not "Not modules". Without such a folder the pack has neither key. The two blocks are fixed data: nothing from the repository is copied into them. The one piece of repository text in the pack is that prefix, and only a word of up to 16 letters and digits is taken;
   - runs `doctor` (step 6) and prints its table;
   - with `--baseline` only: runs each proposed gate once on the base branch in a throwaway worktree in the state dir, and prints a table (gate, command, pass/fail/error, seconds, note). A gate that fails there is written with `baseline: red`, so a run isn't blamed for it, and it leaves the gate sets (if every verify gate is red, they stay, and `init` says runs can't pass verify until you fix them). A missing tool, a timeout or a failed setup is an `error`, never red;
   - writes `.agentflow/config.yaml` once, with `gates_confirmed: false` and `agents.claude.permission_mode: bypassPermissions` (with a comment saying what it allows), and nothing else in the repository.

   It refuses when the repository already has a pack: edit it, or `rm -rf .agentflow` to start over.
3. **Read the notes and the gate table.** The gates are a proposal, and the quality bar is yours to decide: fix the commands, add the gates CI doesn't show, and fix or delete a red gate. `agentflow config show --effective gates` shows the gates together with the starter's.
4. **If you use the bug lane** (on by default), add the `Bash(...)` rule `init` printed to `permissions.allow` in `.claude/settings.json` (shared) or your git-ignored `.claude/settings.local.json`. The bug lane's red/green check runs the regression test only when an allow rule covers it, and `validate` warns until one does.
5. **Set `gates_confirmed: true`** in the pack. `validate` warns until you do.
6. **Check:** `agentflow doctor` (agent CLIs and logins, `gh`, `origin`, Docker, the state dir, each gate's programs), `agentflow validate` (it must print 0 warnings), `agentflow explain feature --module <id>`, and `agentflow run feature --module <id> --dry-run`. Then commit the pack in a PR of its own, so the team reviews the gates and the permission line.

Prompts: put any prompt you want worded differently in `.agentflow/prompts/<name>.md`; it replaces the engine's file of the same name. Skills: a step's skill text is its brief, read from the repository's `project.skills_dir` first and the engine's bundled `agentflow-*` skills second; map your own skills per stage in `skills.map` (section 14). A repository with no skills dir runs on the bundled ones.

Runs, workspaces and worktrees live in the state dir, outside the repository unless `paths.state_dir` puts them inside it; then add that directory to `.gitignore` (`agentflow validate` warns until you do). Removing agentflow from a repository is `rm -rf .agentflow`.

## 14. Pack reference

The pack is the repository's `.agentflow/` directory in the main checkout: `config.yaml`, and `prompts/` for any prompt that replaces the engine's file of the same name. `agentflow` never reads a linked worktree's copy. The engine adds `.agentflow/**` to every pack's forbidden paths and refuses a pack without a non-empty `gate_sets.verify`. One line per key: name, meaning, and whether the Settings page may change it.

**Layers.** `config.yaml` is read over the engine defaults (`agentflow/defaults/config.yaml`), and under your own user file (`$XDG_CONFIG_HOME/agentflow/config.yaml`, else `~/.config/agentflow/config.yaml`), bottom-up: engine defaults, then the starter the pack names (if any), then the pack, then the user file. Mappings merge key by key. A list or a value replaces the one below it, and each entry of `gates` and `conditions`, `record_step` and `resolve_step` is replaced whole. `null` deletes a key from every layer below, and `version` and `engine` are read from the pack file alone. So a pack states only what differs from the defaults, and every key the defaults supply is optional in it. The defaults file carries the minimum pack's values for the keys the code has no fallback for (`effort.default`, `effort.allowed` and the required `context.*` keys: `autocompact_pct`, `rollover_pct`, `max_rollovers`, `progress_file` and `gate_output_lines`), and the engine's own fallback for the other optional ones:
- In `project`: `base_branch` and `fork_from` (`main`), `branch` (`flow/{lane}/{slug}`), `skills_dir` (`.claude/skills`), `commit_trailer` (empty) and `docs` (`specs: docs`, `plans: docs/plans`). There is no default `worktrees_dir`: without one a run's worktree is in the state dir's `worktrees/`.
- In `agents`: `claude` (its command, arguments, models, `effort_arg`, prices and `skill_loading`, with no `permission_mode` and no `max_turns`) and a disabled `codex`. A pack states `agents.claude.permission_mode` itself.
- In `defaults`, `recovery`, `effort` and `context`: every key (no `defaults.env`), at the value the engine falls back to where it has a fallback and at the minimum pack's value where it has none, and `defaults.agent: claude`.
- `guardrails` (empty lists), `hitl` (empty) and `dashboard` (`127.0.0.1`, port 7788). `gates` and `gate_sets` are empty: a pack names its own gates.
- **The workflow:** `lanes` (`feature`, `bug`), `phases`, `conditions`, `record_step`, `resolve_step`, `phase_approval`, `checkpoints`, `autonomy`, `ci` and `skills`. Every step's skill is a `@skills.map.<key>` reference to one of the bundled skills, and its text is the step's brief. A pack that writes `phases:` with `steps` writes the whole workflow (and `lanes`), and the defaults' workflow is dropped. A pack that writes only a phase's `needs_approval`, `resolve`, `model` or `agent` adjusts the defaults' workflow.

The user file holds personal settings only: `agents.<name>.models` for an agent a lower layer defines, `defaults.run_budget_usd`, `defaults.run_budget_hard_usd`, `defaults.task_budget_usd`, `hitl.notify`, `hitl.remind_after_h` and `dashboard.port`. Any other key, a key that is not text, or a file that is not a mapping is an error naming the file and the key. Nothing in it can set a command, a gate, a guardrail or the workflow.

`agentflow config show --effective [PREFIX]` prints every value with the layer it came from. The Settings page shows the same values, marks one that is not the pack's with `from engine`, `from starter:<name>` or `from user`, and saves into the pack only what the pack must state: for a mapping, the entries that differ from the layers below it or that the pack already states (an inherited entry is removed with `<name>: null` in the YAML tab). A value your user file sets is read-only on the page, so a personal value never lands in the committed pack.

A live run sees a change to any layer file between its phases: the runner takes the new config when it loads, validates and keeps the phases, and records `config_reload_skipped` with the reason when it does not (a broken user file is skipped like a broken pack). `run_options` records the digest of the layer files at the start (`layers_sha256`, and each file's own in `layer_digests`). On `resume`, if any of them changed since, one line names the files, and the run goes on with the files as they are now: `config_trust` still decides which pack file is trusted, by its own hash. The trade-off: an engine upgrade that changes the default workflow is reported on resume, not refused. A run started before this record existed reports nothing.

- `version`: the pack format, `2`. The engine reads format 2 only and refuses any other. Protected.
- `engine`: the engine versions that may run the pack, comma-separated clauses of `>=`, `>`, `<=`, `<`, `==` on dotted numbers, such as `">=2,<3"`. A range that leaves this engine out is refused. Protected.
- `starter`: `python-uv`, `node-pnpm`, `go` or `docs-only`; absent or `null` means none. It names a layer the engine ships (`agentflow/starters/<name>/config.yaml`) between the engine defaults and the pack: it supplies `gates` and `gate_sets.setup`, `build` and `verify` for that kind of repository, and nothing of the workflow, so the lanes stay the defaults. The pack overrides it key by key, as it does the engine defaults: a `gates.<name>` it states replaces the starter's whole, and a `gate_sets` list it states replaces the starter's list. Any other value, a path included, is refused, and the error lists the names. `docs-only` has one gate, `whitespace`, which runs `git diff --check` of every tracked file against the empty tree with `blank-at-eol` off, so a Markdown line break is not flagged. Git's other defaults still apply to every tracked file: `blank-at-eof` and `space-before-tab` flag a file that ends in a blank line, or a space before a tab in the indent, so a repository with such a file fails `build` and `verify` on adoption until the file is fixed. The gate doesn't see untracked files, and a docs repository should add a real gate, such as a Markdown linter. Protected.
- `project`: branches (`base_branch`, `fork_from`, `branch`), `worktrees_dir`, the `docs` paths and the `commit_trailer`. Protected.
- `project.worktrees_dir`: where a run's worktree is made. Unset (the default): the state dir's `worktrees/`, outside the repository. Set: a directory relative to the repository root, without `..` (an absolute path or one that leaves the repository is a `validate` error). Protected, as `project` is.
- `project.skills_dir`: the directory holding `<skill>/SKILL.md` for every skill a step names, relative to the repository root and without `..`; default `.claude/skills`. A skill name is a directory name (letters, digits, `.`, `_`, `:` and `-`, not starting with `-`), never a path. A name found in no `skills_dir` falls back to the engine's bundled `agentflow-define`, `agentflow-plan`, `agentflow-build`, `agentflow-debug`, `agentflow-verify`, `agentflow-review` and `agentflow-respond`, so a repository's own skill of the same name wins. Protected.
- `project.docs`: `specs` and `plans` (directories) are required; `idea`, `decisions` and `glossary` are optional, and a step with `checks:` needs `idea` and `decisions`. `{idea}` is empty when `idea` is unset. `agentflow next` reads uncommitted changes under only these three as a design-doc change. The module list is not here: see `work_source`. Protected.
- `project.docs.spec_file`: a module's spec file name inside `docs.specs`, with `{module}` in it and no other placeholder (write a literal brace as `{{` or `}}`); default `{module}.md`. `agentflow next` reads every file in `docs.specs` that fits it as a module's spec. Protected.
- `project.docs.done_status`: a regex that marks a module done ("spec says done") when it matches a line of its spec's header on `origin/<fork_from>`; default `^[-*>\s]*Status[\s:*]+(done|shipped|implemented)(?![\w-])`. Only header lines count (those before the first `## ` heading, outside code fences; a line longer than 512 characters never counts), matched case-insensitively, so `Status: done`, `**Status:** done`, `**Status: shipped** 2026-01-02`, `- **Status:** done` and `> Status: done` count and `Status: done-ish` or `Status: not done` do not. The spec is read from `refs/remotes/origin/<fork_from>`, never the work tree, so unmerged work never counts. A veto (`agentflow backlog undone <module>`) overrides it. A pattern that does not compile is a `validate` error. Protected, as `project` is.
- `paths.state_dir`: where runs (`runs/`) and agent workspaces (`workspaces/`) live; `~` is expanded and a relative path is relative to the main checkout. Default `${XDG_STATE_HOME:-~/.local/state}/agentflow/<repo-id>`, outside the repository (`<repo-id>` is the checkout's name and a hash of its path). A state dir inside the repository that git does not ignore is a `validate` warning. Protected.
- `work_source`: where the modules `agentflow queue`, the Backlog and the `modules_done` check walk come from. Optional: without it `{capability_map}` is empty, `agentflow next` skips the module order, the Backlog shows only the cards added on the page, and `agentflow queue` refuses to run. Protected. Two kinds, by `kind`:
  - `capability_map` takes `path` (relative to the repository root): a Markdown table with `Module id` and `Depends on` columns, one row per module in build order; `{capability_map}` is its path.
  - `specs` takes no `path`: the directory is `project.docs.specs`, and each file there whose name fits `project.docs.spec_file` is one module. Its dependencies come from the `Depends on:` lines before the spec's first `## ` heading (`**Depends on:** a, b`, `- Depends on: a`), with the bullets or comma list that follow one ending in `,` or nothing; only `none`, `none.` or `none (…)` means none, and an unknown dependency blocks. A `Depends on:` with no value blocks too, as does a spec over 8 KB with no `## ` heading in its first 8 KB, since its header may go on past what is read. Under the bare `{module}.md`, `index`, `readme`, `template` and `changelog` are not modules. A name that is not a lowercase slug, a symlink, a directory and an unreadable file are skipped and shown on the Backlog. `{capability_map}` is empty, a new module is a new spec (no row is written), and `agentflow queue` needs `--include-specs`. `validate` reports a `path`, a `project.docs.specs` that is not a directory inside the repository, and a missing `dashboard.backlog.module_lane`.
- `skills`: `skills.map` names the skill `agentflow next` suggests for each kind of next step: `decide`, `define`, `design_doc`, `plan`, `build`, `build_small`, `verify`, `ship`, `review`, `debug` and `review_feedback`. Another key is an error. `review` and `debug` name the skill for a review and for a bug fix, and `review_feedback` the skill sent with blocking per-task findings. Each must be a skill in `project.skills_dir` or one of the engine's bundled skills (a name that is not a skill name is an error). Optional: a step with no entry is named by its stage (`Build stage`). Protected.
- `agents.<name>`: the command line and arguments of an agent CLI, its models, `max_turns` and prices. Protected, except `enabled`, `models`, `max_turns` and `prices`.
- `defaults`: the default agent, timeouts, `fix_attempts` and budgets. Not protected, except `defaults.env` and `defaults.env_inherit`.
- `defaults.run_budget_usd`: the soft budget in USD, 25 by default, recorded when a run starts (`soft_budget_usd`). Crossing it sends one notification (`budget_soft_reached`, once per soft value) and never stops the run. A plain approve of a `budget_exceeded` stop adds this much to the spend so far as the new hard budget. A finite number above 0, or `validate` refuses it. If you set it above 65, also set `run_budget_hard_usd`, because that must be at least this. Not protected; the user file may set it.
- `defaults.run_budget_hard_usd`: the hard budget in USD, 65 by default, recorded when a run starts (`budget_usd`). Crossing it stops the run with `budget_exceeded` for a human's approval; it is the only budget stop. A finite number above 0 and at least `run_budget_usd`, or `validate` refuses it (so a `run_budget_usd` above this default needs its own `run_budget_hard_usd`). Plan sizing never raises it. Not protected; the user file may set it.
- `defaults.task_budget_usd`: USD per open plan task, 2 by default; 0 turns sizing off. When a feature run's `build` starts, the soft budget grows once to the spend so far plus this for each unticked task plus a quarter of `run_budget_usd` (`budget_scaled`, with `soft: true`). It never lowers a budget, never raises the hard one, and a budget a human set is kept; an estimate above the hard budget sends one notification (`budget_estimate_over_hard`) and the run goes on. Negative is a `validate` error. Not protected; the user file may set it.
- `defaults.env_inherit`: which variables of the process that started agentflow a run's agent sessions and gates do not inherit. `drop` is a list of variable names or `fnmatch` globs (`*_URL`), empty by default. `drop_dotenv` is a list of repository-relative files, `[".env"]` by default, with no leading `/` and no `..`: a variable with the same name **and** the same value as a line of such a file in the main checkout was almost certainly loaded by an IDE or shell and is dropped, while one with another value is kept because you set it on purpose. `validate` refuses a bad glob or path. `defaults.env` always wins over a drop, and `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TMPDIR`, `LANG`, `LC_*` and `TERM` are never dropped. The dotenv files are read without following a symlink, up to 256 KB. A run records the dropped names (never a value) as an `env_dropped` event, again only when the set changes on a resume. Protected, so the Settings page and the user file cannot change it.
- `recovery`: how long an outage may last and how many failed attempts a step gets. Not protected.
- `effort`: the level a session starts at and the levels an agent may pick. Not protected.
- `context`: auto-compact, rollover and the context window. Not protected, except `context.autocompact_env` and `context.progress_file`. `autocompact_pct` (60) is read by Claude Code against `context_window_tokens` (200000), which the runner passes as `CLAUDE_CODE_AUTO_COMPACT_WINDOW`: without that cap the models' own 1M window makes "60%" 600k tokens, and nothing compacts before the runner's `rollover_pct` (80, of the same window) ends the session. `validate` wants a number from 100000 to 1000000 or `auto` (no cap, Claude's own window), `rollover_pct` above `autocompact_pct`, and `max_compactions` (3) of at least 1. After `max_compactions` compactions the runner replaces the session with a fresh one seeded from `progress.md`, which counts against `max_rollovers` (2).
- `gates`: names to shell commands, with `cwd`, `when_exists` and `baseline`. Protected.
- `gates_confirmed`: `true` once a human has checked `gates` and `gate_sets`, whether the pack wrote them or inherited them from a starter or the engine. Anything but `true` (absent included) is a `validate` warning, "detected or inherited gates are a proposal"; a value that is not a bool is an error. It is a bool, not bound to the commands that were confirmed: when an engine upgrade changes a starter's gates, `validate` does not ask again, so the release notes of an engine that changes a starter must say so. Protected, so the Settings page cannot confirm for you.
- `gates.<name>.baseline`: `red` records that the gate failed on `project.base_branch` when the pack was set up, and is a `validate` warning ("`<gate>` failed on `<base_branch>` when the pack was set up: fix it and remove `baseline`, or delete the gate"); `green` is allowed; any other value is an error. Protected, as `gates` is.
- `gates.<name>.cwd`: the directory a gate runs in, relative to the run's working directory (the worktree root when unset). It must be relative with no `..` part, otherwise `validate` refuses it. Protected, as `gates` is.
- `gates.<name>.agent_check`: `true` lists the gate in the build prompt as a check the implementer runs itself before writing its result (the `{self_checks}` placeholder). Only for a fast gate, such as a linter, a formatter check or a type checker, never a whole test suite. The runner still runs the gate afterwards. It needs a one-line `run` with no backtick and no `cwd`, otherwise `validate` refuses it. Default `false`. Protected, as `gates` is.
- `gate_sets`: named lists of gates; `verify` is required. Protected.
- `guardrails`: `forbidden_paths`, `reject_trailers` and `user_only_skills`. Protected.
- `overlays`: the `security` and `eval` paths, keywords and added skills. Protected.
- `conditions`: named checks the runner evaluates from files and git; `modules_done` reads `work_source` and takes no `from`. `finish` also runs the `eval` gate on a merged tree when the pack defines `eval_required` and it holds. Protected.
- `lanes`: the kinds of work. Not protected. When it includes `bug` and `.claude/settings*.json` in the main checkout holds no `Bash(...)` allow rule at all, `validate` warns at `lanes`: the bug lane's red/green check runs the regression test only when a Bash allow rule covers it, whatever `permission_mode` says. Add a narrow rule such as `Bash(<verify gate's program> *)`.
- `record_step`, `resolve_step`: the steps that file a spec-gap answer and resolve a stop. Protected.
- `autonomy`: what the runner decides by itself. Not protected.
- `autonomy.answer_recommended`: `true` (the default) lets the runner take a `needs_human` result's recommended answers in **every** phase, `resolve: true` or not, parked or not, when each question has exactly one recommended option and none has a kind in `human_question_kinds`. It records them as `hitl_answered` with `by: runner`, notifies one line ("took the recommended answer(s) q1=A in plan/plan; the decision is recorded"), files a `spec_gap` answer in the spec through `record_step` as a human's answer is, and lists each one under "Decisions the runner took" in the pull request. A step that asks a question the runner already answered on it goes to a human. `false` sends every question to a human. A value that is not a bool is a `validate` error. Not protected.
- `autonomy.human_question_kinds`: the question kinds only a human answers, `[boundary]` by default (`boundary`: the repository's docs reserve the decision for a human, such as an "ask first" or "never" rule). One such question makes its whole set a human's in any phase, parked or not. `[]` lets the runner answer every kind. A list of the kinds `spec_gap`, `ambiguity`, `conflict`, `review_dispute`, `boundary` and `other`, or `validate` refuses it. Not protected.
- `ci`: polling and fix rounds of the `ci` step. Not protected.
- `phases.<name>`: the workflow. Protected, except `needs_approval`, `resolve`, `model` and `agent`.
- `phase_approval`: the refine prompt and what an approval page shows. Protected.
- `checkpoints`: who answers each inner checkpoint. Not protected.
- `hitl`: notifications, reminders and where decisions are recorded. Not protected, except `hitl.notify`.
- `dashboard`: host, port and the Backlog. Not protected, except `dashboard.host`.
