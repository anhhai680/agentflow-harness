# Agentflow: config-driven automated workflows over the repo's agent skills

> **Superseded in part, 2026-10-31.** Agentflow now has one main workflow (`define → plan → build → verify → review → ship`), and lanes select its steps. The `pipelines:` and `lanes:` step libraries below no longer exist, and the command is `agentflow`, not `flow`. `devtools/agentflow/config.yaml` and `docs/agent-orchestration-guide-v3.md` section 11 are current. The rest of this document is kept as the design history.

## Context

`docs/agent-orchestration-guide-v3.md` settled *which* skill owns each SDLC stage and the lanes (design-doc, feature, bug, eval, security overlay), but every hop is still driven by hand in an interactive session: the developer reads the `Next:` footer, types the next prompt, runs gates, commits. All 13 module specs are now approved (CAPABILITY-MAP build order: `platform → staff-auth, crm → settings → …`), so the bulk of the 15-week MVP is about to be Plan → Build → Verify → PR, repeated per module slice. That is the part worth automating.

Goal: a local runner, **`agentflow`**, that executes the CLAUDE.md lanes headlessly with Claude Code (`claude -p`) — Codex supported by config but off — where **everything that might change lives in one `config.yaml`**: agents, the model for each phase (falling back to one per role), the effort range agents may choose from, lanes and their steps, which skill each step loads, gates (the CONSTRAINTS.md commands), human checkpoints, guardrails, budgets. Decisions from the user:

- **Autonomy:** commit per task, push the branch, open a **draft PR**. Humans approve every phase before the next one starts (a rejection sends the whole phase back to be refined) and merge PRs. The runner never adds `Constraints-Approved-By`.
- **Agents:** Claude only by default; the Codex adapter exists, `enabled: false`.
- **Runtime:** local CLI now; GitHub Actions later, when SPEC-platform creates `.github/workflows/`.
- **Lanes:** feature, bug, eval, design-doc (design-doc pauses for human answers).

Design principles (from the guide, CLAUDE.md and CONSTRAINTS.md):
1. **The skills stay the brains; the runner is plumbing.** Each step's prompt says "use the `<skill>` skill"; the runner adds no second router and never loads a second skill for a stage.
2. **Gates, not claims.** A step is done when its configured shell gates pass, not when the agent says so (`verification-before-completion`, made mechanical).
3. **One review per task, one per branch** — the runner *is* the external form of `subagent-driven-development`: fresh implementer per task, one reviewer per task, one final branch review.
4. **User-only skills never run automatically** (`improve-codebase-architecture`, `constraint-driven-development`, `doubt-driven-development`, `using-git-worktrees`, `writing-for-agents`); they appear only as checkpoints that ask the human.
5. **State in files, resumable.** Any run can stop (checkpoint, failure, Ctrl-C, budget) and `resume`.

## Phase chaining: who decides what runs next

**The agent never decides the next phase, and never has to "continue".** Each agent call does exactly one step and exits. The runner is a loop: when a phase's steps are done and their gates pass, it **stops for a human to approve the phase** (see Phase approval). Only after an approval does it evaluate the config's transitions and start the next phase in a fresh agent session, handing over through files (spec, plan, `handoff.md`). A rejection never moves forward: every step of the phase runs again with the human's note. So `brainstorm → spec → design → plan → build → test → review → finish` runs as one command that pauses at the end of every phase that ran, and also wherever a step needs a human (HITL) or something fails.

```
flow run feature --module crm
  ┌─────────── runner loop (flow.py) ───────────┐
  │ pick phase ← start_at: auto (repo state)     │
  │ for each step: check when/unless → agent -p  │──► claude -p (1 step, exits)
  │   gates ✓ → write handoff.md → transitions   │◄── result.json + files
  │   gates ✗ → fix loop → HITL                  │
  │ phase done → ⏸ <phase>_approved (human)      │
  │   approve → next phase                       │
  │   reject  → refine every step of the phase   │
  └──────────────────────────────────────────────┘
```

### `pipelines:` in config.yaml

A pipeline is an ordered list of phases; a phase is a named group of steps (the step lists under `phases:`, written like the lanes below). Order is the default transition; `next:` overrides it with conditions.

```yaml
pipelines:
  feature:                                   # flow run feature --module crm
    start_at: auto                           # or a phase name; --from/--to on the CLI override
    phases: [brainstorm, spec, design, plan, build, test, review, finish]
  bug:      {start_at: debug, phases: [debug, regression_test, fix, test, review, finish]}
  eval:     {start_at: change, phases: [change, eval_gate, review, finish]}
  decision: {start_at: grill, phases: [grill, apply_decision, commit]}   # design-doc lane

phases:
  brainstorm:   # superpowers brainstorming was dropped; the repo's Decide owner is `grilling`
    model: opus
    skip_if: spec_exists                    # existing module → straight to spec
    steps: [{skill: grilling, role: plan, prompt: brainstorm.md, output: "runs/{run}/questions.md"},
            {type: checkpoint, name: brainstorm_answers}]
  spec:         # = the spec lane below (draft unless exists, grill open questions, approval)
    model: opus
    skip_if: spec_is_approved
    steps: "@lanes.spec.steps"
  design:       # contracts and module seams, only when needed
    model: opus
    steps: [{skill: api-and-interface-design, role: plan, when: spec_defines_routes},
            {skill: codebase-design, role: plan, when: new_module_package}]
    skip_if: {not: {any: [spec_defines_routes, new_module_package]}}
  plan:         {model: opus, skip_if: plan_exists_unchecked, steps: "@lanes.feature.plan_steps"}
  build:        # per task: TDD + task review + commit
    model: {default: sonnet, review: opus}  # implementer on sonnet, the per-task reviewer on opus
    steps: "@lanes.feature.build_steps"
  test:         {model: sonnet, steps: [{skill: verification-before-completion, role: build, gates: verify, fix_loop: true}]}
  review:       {model: opus, steps: [{skill: requesting-code-review, role: review, prompt: branch_review.md}]}
  finish:       {steps: [{type: finish}]}                    # push + draft PR; no agent, so no model
  # bug/eval/decision phases are defined the same way from their lanes below, each with its own model:
  debug:           {model: opus,   steps: "@lanes.bug.steps.debug"}
  regression_test: {model: sonnet, steps: "@lanes.bug.steps.regression_test"}
  fix:             {model: sonnet, steps: "@lanes.bug.steps.fix"}
  change:          {model: opus,   steps: "@lanes.eval.steps.change"}      # prompt/retrieval edits
  eval_gate:       {model: sonnet, steps: "@lanes.eval.steps.eval_gate"}   # only the fix loop calls an agent
  grill:           {model: opus,   steps: "@lanes.design.steps.grill"}
  apply_decision:  {model: opus,   steps: "@lanes.design.steps.apply"}
  # a phase can also move to another agent: {agent: codex, model: gpt-5-codex, …}
```

### Model and effort per step

**Model: set per phase in config.** Each step resolves its model like this, first hit wins:

1. `--model <phase>=<model>` on the CLI (one run only, e.g. `flow run feature --module crm --model plan=fable`).
2. `model:` on the step itself (rare; for a step that differs from the rest of its phase).
3. The phase's `model:`, either a string for every agent step in the phase, or a map by step role with a `default` (as `build` does, so its reviewer runs on a different model than its implementer).
4. `agents.<agent>.models[<role>]`: the fallback for phases that set no model, and for agent calls that belong to no phase (an `on_unknown: agent` check uses the `mechanical` role).

The model always belongs to the phase's agent (`agent:` on the phase, else `defaults.agent`), so a phase moved to Codex names a Codex model. `flow validate` fails when a phase names a model for a disabled agent, or sets a role map without `default`. `flow explain` prints the resolved model next to each step.

**Effort: the agent picks it, within low–high.** No phase or step names an effort level, and the runner doesn't judge one either. The agent doing the step knows best how big and hard the task is, so it decides:

- **Every agent session starts at `effort.default` (medium).**
- **The agent sizes the task first, once per step.** Every prompt template starts with an effort check. After reading its inputs (the skill, the spec section, the plan task and the files it names), the agent decides whether medium fits. If it doesn't, the agent stops before doing any work and writes `result.json` with `status: effort_change`, the level it wants, and why. Examples: a one-line config tweak goes down to low, and a 6-file retrieval change goes up to high.
- **The runner starts the step again at that level.** No work has been done yet, so the sizing session is dropped and a new session gets the same prompt at the requested level, marked as already sized so it doesn't run the check again. The runner accepts only the levels in `effort.allowed` (low, medium, high). A request outside that range, such as `xhigh` or `max`, is refused, and the step goes on at medium with a note saying so.
- **Every resume keeps the step's effort.** The runner owns the level: it stores it in `state.json` and passes `--effort <level>` explicitly on every call, never relying on the CLI to remember it. That's needed because `--effort` applies only to the invocation it's given on, and the stream-json `init` event doesn't report the level (only `per_turn_effort_active`), so the runner's record is the only source. Checked on Claude Code 2.1.312 on 2026-10-29: `claude -p … --resume <id> --effort high` on a session started at `--effort low` exits 0 and keeps the same session id. The level is passed on every resume: fix-loop attempts, review findings sent back to the implementer, a resume after a human answer, each step of a refine round after a rejection, `flow resume`, `flow attach`, and the fresh session after a rollover. A resume never changes the level, so an `effort_change` sent after work has started is refused and logged, and the agent carries on.
- **Each step starts fresh at medium.** The next step's agent sizes its own task.

```json
{"status": "effort_change", "effort": "high",
 "reason": "Task 5 rewrites the RRF fusion query across 6 files in knowledge/retrieval/; the ranking edge cases need careful reasoning."}
```

```yaml
effort:
  default: medium               # every step's sizing session starts here
  allowed: [low, medium, high]  # the only levels an agent may pick; resumes reuse the step's level
```

Every switch is logged as an `effort_changed {from, to, reason}` event, with the agent's reason. It's shown in `status`, on the dashboard card and in the PR body, so you can see where agents chose high. The adapter turns the level into each CLI's flag through `effort_arg`: Claude Code's `--effort <level>`, and Codex's `-c model_reasoning_effort=<level>`.

### How the runner picks the phase

- **`start_at: auto`** evaluates each phase's `skip_if` in order and starts at the first that doesn't hold — the deterministic form of `next-step`. For `crm` today: brainstorm skipped (spec exists), spec skipped (approved), design runs only if its conditions hold, then plan → build → … `flow explain feature --module crm` shows this before anything runs.
- **Between phases** the runner re-evaluates: e.g. after `plan`, if `plan_is_small` the build phase uses the `executing-plans` style (one session, tasks in sequence); otherwise one fresh implementer + one reviewer per task (`subagent-driven-development` style). Same config, same `when`.
- **Handoff:** each phase ends with the runner writing `runs/<run>/handoff.md` (artifacts produced, decisions made, skipped steps and why, open risks). The next phase's prompt includes it plus the artifact paths, so the fresh session starts with what it needs and nothing else (cheaper than one long session, and survives compaction).
- **Cross-check with the skills' footers:** every skill ends with `Next: <skill> — <reason>`. The runner parses that line and compares it with the phase it is about to start; a mismatch never changes the route (config wins) but is logged in `status` and the PR body, which is how you spot a config transition that is wrong.
- **No phase starts without an approval.** See Phase approval below.

### Phase approval: a human approves every phase before the next one starts

Every phase that ran ends at a human checkpoint named `<phase>_approved` (`spec_approved`, `plan_approved`, `build_approved`, …). The runner adds it itself, so it isn't written in the step lists, and it is never cleared automatically: there is no `auto_if` for it, no config key turns it off, and `flow validate` rejects any attempt. A run can wait at an approval for days; it never times out into an approval.

- **Which phases need one.** A phase ran if at least one of its steps ran. A phase skipped by `skip_if`, or whose steps were all skipped by their conditions, produced nothing, so it raises no approval. The runner logs `phase_skipped` with the reason and moves on, and `explain` shows it.
- **What the human reviews.** The inbox entry and the dashboard card list what the phase produced: `handoff.md` (artifacts, decisions, risks), the diff and commits since the phase started, each step's result summary, skipped steps and why, gate results, and review findings. After a refine round they also show the diff since the last approval request.
- **Approve:** `flow approve <run> <phase>` (or the dashboard button). The runner writes `handoff.md` and starts the next phase in a fresh session.
- **Reject:** `flow reject <run> <phase> --note "…"`. The runner starts a refine round (below), and when it's done it asks for approval again. You can reject as many times as you need. Each round is logged, the run budget still applies, and `flow cancel` stops the run.
- **Phases that already end at a human step** (brainstorm's and the design lane's answers checkpoint): `flow answer` asks for the phase approval right after the last answer, so the human is stopped once, not twice. The answers are part of what gets approved.
- **The last phase has no approval**, because no phase follows it. `finish` opens the draft PR, which humans review and merge on GitHub, and the design lane's `commit` lands a doc change whose `apply_decision` phase was already approved.
- **Inner checkpoints stay as they are.** These are stops inside a phase, configured per checkpoint:

```yaml
phase_approval:                  # raised after every phase that ran; nothing here can skip it
  refine_prompt: refine.md       # template for each step of a refine round
  show: [handoff, diff, step_results, skipped_steps, gates, review_findings]
checkpoints:                     # inner checkpoints, inside a phase
  brainstorm_answers: human      # always: product questions
  doubt_offer:        human      # security paths only
```

### Refine round: what a rejection does

A rejection refines the **whole phase**, not just its last step: every step in the phase runs again, in order, with the note.

1. **Record.** The note goes into `state.json` and `runs/<run>/refine/<phase>-<round>.md`, and the runner logs a `phase_rejected {phase, round, note, by}` event.
2. **Re-evaluate conditions.** `when`/`unless` are checked again, because the refinement may change them (e.g. a spec that now defines routes runs `contracts`). A step that was skipped before and holds now runs fresh; a step that ran before and no longer holds is logged as skipped, and its earlier output stays.
3. **Refine each agent step.** A review step (`role: review`) is the exception: it starts a fresh session, told only the earlier blocking findings and the note. Every other step resumes its own last session (its id is in `state.json`) at the step's recorded effort, with `prompts/refine.md`: the note, the round number, the step's previous `result.json` summary, and the paths of the phase's current artifacts. The agent revises its existing output in place and doesn't start over. If the note doesn't concern its step, it writes `status: unchanged` with a one-line reason, which is shown at the next approval. When a session can't be resumed (expired or rolled over), a fresh session gets the same prompt plus `progress.md` and `handoff.md`, at the same recorded effort and with no sizing check.
4. **In `build`, every task is refined**, in plan order, each in its own task session, as on the first pass. A task that answers `unchanged` costs one short turn.
5. **Same checks as the first pass.** Guards, gates, the fix loop and the per-task review all run again.
6. **New commits only.** Refined work is committed on top as `Refine <step or Task N>: <title>`, with the note in the body. History is never rewritten: no amend, rebase or force push.
7. **Inner checkpoints** (answers, the doubt offer) are raised again only when a refined step writes new questions. Otherwise the earlier answer is reused, and the runner logs that it did.
8. **Ask again.** When every step is done, the runner raises `<phase>_approved` again, now showing the diff since the last request and which steps changed or answered `unchanged`.

- **Across modules:** `flow queue` chains pipelines: when a module's PR is merged (`branch_merged`) the next modules in the CAPABILITY-MAP order whose dependencies are done start automatically.

## Context window management

No agent session lives for the whole workflow. Three layers keep each one small:

| Layer | Rule | Who enforces |
|---|---|---|
| **Session scope** | New session at every phase switch; in `build`, a new implementer session per plan task and a new reviewer session per review. Fix loops and refine rounds resume the same session (they need its memory of the failure or of what it produced); a reviewer never does, so a re-review inherits no earlier verdict. | runner |
| **Auto-compact at 60%** | Every agent process is started with Claude Code's auto-compact threshold set to `context.autocompact_pct` (60) through its env override, so a long step compacts its history instead of running out. Codex gets the equivalent `model_auto_compact_token_limit` in the `agentflow` profile. | Claude Code / Codex, configured by the runner |
| **Rollover safety net** | The runner reads token usage from each stream-json turn; if a session crosses `rollover_pct` (80) anyway (compaction failed or one huge tool output), it lets the current turn finish, ends the session, and continues the same step in a **fresh session** seeded from `progress.md` + `handoff.md`. A step that rolls over `max_rollovers` times → HITL (the step is too big; split the task). | runner |

What makes compaction and rollover lossless:
- **`runs/<run>/progress.md`**: every prompt tells the agent to append a 3–5 line entry after each sub-step (what's done, files touched, what's next, open doubts). It's the memory that survives compaction, rollover and `resume`; the runner checks it was updated (mtime) when a step ends.
- **`handoff.md`** between phases (see Phase chaining) carries only decisions, artifact paths and risks — never transcripts.
- **Prompts pass paths, not contents**: the spec, plan task and gate output are referenced or trimmed (gate output capped to the last `gate_output_lines`, full log in `runs/`). The idea doc is never inlined, following CLAUDE.md (grep a section and read only it).
- **Skills load by name** from `.claude/skills/`, so only the step's skill enters context.
- Right-sized steps are the main lever: `writing-plans` already sizes a task as one testable slice; the runner warns in `explain` when a task's step list or touched-file list looks too large for one session.

```yaml
context:
  autocompact_pct: 60            # → Claude Code auto-compact env override on every `claude -p`
  rollover_pct: 80               # runner safety net
  max_rollovers: 2               # then HITL: split the task
  context_window_tokens: auto    # from stream-json usage/model info; set a number to override
  progress_file: "runs/{run}/progress.md"
  gate_output_lines: 200
```

`flow status` shows each live session's context use (%), compactions and rollovers, so a step that keeps compacting is visible. The exact env var for the auto-compact threshold, and whether usage in stream-json reports the context window, get checked against the Claude Code docs (via the `claude-code-guide` agent / `claude --help`) at task 2; if the threshold isn't settable, the runner's rollover at 60% becomes the primary mechanism.

## Tracking runs: event log + Kanban dashboard

### One source of truth

Every run writes two files, and nothing else is read for tracking:
- `runs/<run>/state.json`: the current snapshot (pipeline, module, phase, step, status, context %, spend, branch, worktree, PR url, waiting checkpoint or phase approval, refine round and its note), plus `agents:`, one entry per live agent process of the run: agent CLI, session id, PID, phase, step id and skill (task N/M in build), role, model, effort and whether the agent changed it, status (`sizing · running · compacting · waiting_human`), context %, spend, start time, last activity.
- `runs/<run>/events.jsonl`: append-only events — `run_started`, `phase_started`, `step_started`, `step_skipped {condition, reason}`, `agent_started {agent, session_id, pid, phase, step, role, model, effort}`, `agent_turn {session_id, tokens, context_pct, cost, last_tool}`, `subagent_started|subagent_finished {parent_session, subagent_type, model}`, `agent_exited {session_id, exit_code, cost}`, `effort_changed {from, to, reason}`, `compacted`, `rolled_over`, `gate_result {gate, passed, skipped, log}`, `review_result`, `hitl_raised {kind, questions}`, `hitl_answered {by}`, `phase_approval_raised {phase, round}`, `phase_approved {phase, by}`, `phase_rejected {phase, round, note, by}`, `phase_skipped {phase, reason}`, `step_unchanged {step, reason}`, `commit {sha}`, `pr_opened`, `run_finished|failed|cancelled`.

The CLI, the dashboard and the PR body all render from these, so they can't disagree. A run in progress also holds a lock file with the runner's PID; a dead PID shows the run as `stalled` (resumable).

### Dashboard: `flow dashboard`

A local Kanban board at `http://127.0.0.1:7788/?token=…`:

```
 Backlog     Brainstorm  Spec   Design  Plan        Build            Test    Review   Finish     Done
 (queue)                                                                                (draft PR)
┌────────┐                              ┌─────────┐ ┌──────────────┐
│settings│                              │staff-auth│ │crm           │
│waits on│                              │⏸ plan_   │ │Task 3/7 ▸TDD │
│platform│                              │ approved │ │sonnet · high↑│
└────────┘                              │no agent  │ │ctx 42% $3.10 │
                                        └─────────┘ │gates ✓ floor │
                                                    └──────────────┘
 ─── Needs human (swimlane across all columns) ───────────────────────────
 staff-auth · plan_approved · 12 min ago   [Approve] [Reject…] [Open plan]
 crm · spec_gap q1 "Cache MISA token across conversations?"  [Answer…]
```

- **Columns** = the pipeline's phases (from config, so a new phase adds a column); **cards** = runs, one per module/bug/eval task; **Backlog** = `queue` modules not started, with what they're waiting on (CAPABILITY-MAP deps).
- **Card face:** module, current step (and task N/M in build), status badge (running · waiting human · awaiting approval · refining · failed · stalled), the refine round when one is running (`refine 2`), the live agent's model and effort (`↑`/`↓` when the agent changed the level, its reason on hover; `no agent` while the run waits or runs a gate), context %, spend vs budget, last gate results, time in phase.
- **Card detail:** the step timeline with skipped steps and their reasons, gate logs, review findings, `progress.md`, `handoff.md`, links to spec/plan/branch/PR, and the agent transcript tail.
- **Actions** (call the same functions as the CLI): approve a phase / reject it with a note (which starts a refine round), answer questions (the inbox form), pause, resume, cancel, retry step, copy `flow attach <run>` (an interactive session opens in your terminal, not the browser). Every action is an event with who did it.
- **Header:** runs by status, agents running (by agent CLI and model), today's spend vs `run_budget_usd` totals, notifications toggle, and a Board / Agents switch.

**Agents view.** The board shows one card per run; the Agents view shows every agent process running right now, across all runs and worktrees, one row each:

```
 Agents running: 3  (claude 3 · codex 0 | opus 1 · sonnet 1 · haiku 1)            [Board] [Agents]
 Run       Pipeline · phase › step                       Role    Agent   Model   Effort    Status     Ctx  Spend  For  Last activity
 crm       feature · build › task 3/7 implement (TDD)    build   claude  sonnet  high ↑    running    42%  $3.10  6m   Edit src/tivacare/crm/client.py
   └ sub   Explore (spawned by the session above)        —       claude  haiku   inherit   running     —     —    1m   Grep "customer_code"
 settings  feature · spec › grill_open_questions         plan    claude  opus    medium    sizing      9%  $0.20  1m   Read docs/specs/SPEC-settings.md
 ─── exited in the last 10 min ──────────────────────────────────────────────────────────────────────────────────────────────
 crm       feature · build › task 2/7 review             review  claude  opus    medium    exit 0     31%  $0.90  4m   wrote result.json (done)
```

- **Phase › step** comes from the run's state: pipeline, phase, step id, the skill it loads, and task N/M in build. The fix-loop attempt (`fix 2/3`) and rollover count are appended when they apply.
- **Model** is the resolved model the runner passed (see Model and effort per step), with its source on hover: CLI override, step, phase, or the agent's role fallback.
- **Effort** is the level on the current call. `↑`/`↓` marks a level the agent chose in its sizing check, with its reason on hover; a refused request (`xhigh`, `max`, or a change after work started) shows as a note. While the sizing check runs, the status is `sizing`.
- **Subagents** a session spawns itself (its Agent tool calls in stream-json) are nested under it with their type and model, or `inherit` when the stream doesn't report one. Which fields stream-json carries for subagents is checked at task 2; if it reports none, only the count shows.
- **Last activity** is the latest tool call or turn from the agent's stream, so a stuck agent is easy to spot; a row with no activity for `dashboard.agents.idle_warn_min` turns amber.
- A row links to the run's card detail at that step (transcript tail, progress.md). Rows leave the live list when the process exits (`agent_exited`) or its PID is gone, and stay under "exited" for `show_exited_min`.

Implementation stays dependency-light: `http.server` from the stdlib serving one static `dashboard.html` (vanilla JS, no build step), a JSON API over `state.json`/`events.jsonl`, and Server-Sent Events from the event files for live updates. It binds to `127.0.0.1` only and requires the random token printed at start, because it can approve checkpoints and start agents.

For the terminal: `flow status --watch` prints the same board as a compact table (phase columns, one row per run) and refreshes on new events; `flow agents --watch` prints the Agents view; `flow inbox` lists only the Needs-human items.

```yaml
dashboard:
  host: 127.0.0.1
  port: 7788
  columns: from_pipeline          # or an explicit list
  show_done_days: 7
  agents: {show_exited_min: 10, idle_warn_min: 5}
```

## Layout

```
devtools/agentflow/
  config.yaml          # the only file you edit to change behaviour
  flow.py              # CLI entry; PEP 723 header: dependencies = ["pyyaml>=6"]
  config.py            # load + validate config.yaml (typed dataclasses, clear errors)
  agents.py            # ClaudeAgent / CodexAgent adapters behind one interface
  gates.py             # run named gates, when_exists skipping, output capture
  guards.py            # forbidden-path diff check, trailer check, user-only skill check
  conditions.py        # built-in checks, all/any/not, on_unknown, explain
  state.py             # runs/<run-id>/state.json, checkpoints, resume
  hitl.py              # result.json schema, inbox files, notify, answer/attach, decision recording
  events.py            # events.jsonl writer/reader, run lock + stalled detection
  dashboard.py         # stdlib http.server: JSON API, SSE, actions → same functions as the CLI
  dashboard.html       # single static Kanban page, vanilla JS
  sources.py           # plan parser (### Task N + - [ ]), capability-map DAG, spec status
  lanes.py             # step executor: foreach, fix loop, review, commit, finish
  prompts/             # one Markdown template per step ({placeholders})
  tests/               # pytest with a fake agent command
  runs/                # git-ignored: state, logs, agent transcripts, questions files
AGENTS.md              # repo root, for Codex: "Follow CLAUDE.md; skills are in .claude/skills/<name>/SKILL.md"
```

Run with `uv run devtools/agentflow/flow.py <command>` (works before the uv project exists, like `floor_guard.py` runs before it). `devtools/` is already out of coverage; the floor guard still applies to it.

## CLI

| Command | What it does |
|---|---|
| `validate` | Parse config.yaml, check skills/prompts/gate cwd exist, list user-only skill misuse |
| `next` | Deterministic port of `next-step` §1–2: prints `Lane · Stage · Evidence · Next` |
| `run feature --module crm [--slice "weeks 1–2"] [--from plan] [--to test]` | The whole feature pipeline, phase after phase, starting where the repo state says (or `--from`), and stopping for approval at the end of every phase that ran |
| `run spec --module crm` | Only the spec phase: draft or amend a spec, contracts and grilling only when their conditions hold |
| `explain <lane> --module <id>` | Evaluate conditions and print which steps would run or skip, and why, with each step's model |
| any `run …` `--model <phase>=<model>` | Override a phase's model for this run only |
| `run bug --issue 42` / `--log path` | Bug lane |
| `run eval --task "…"` | Eval-gated change lane |
| `run design --topic "…"` | Design-doc lane (pauses for answers) |
| `queue [--max-parallel 2]` | Walk the CAPABILITY-MAP build order: start every module whose deps are done, each in its own worktree |
| `status [run-id] [--watch]` | Runs, current phase/step, checkpoint waiting, context %, spend; `--watch` = live terminal board |
| `dashboard` | Local Kanban board (phases as columns, runs as cards, Needs-human swimlane) and the Agents view (every running agent with its phase › step, model and effort) |
| `agents [--watch]` | Every agent process running now, across runs: run, phase › step, role, agent, model, effort, status, context %, spend, last activity |
| `pause` / `cancel` / `retry <run-id> [step]` | Control a run |
| `inbox` | Every run waiting on a human, with its questions |
| `answer <run-id>` / `attach <run-id>` | Answer an agent's questions, or open its paused session interactively |
| `approve` / `reject <run-id> <phase\|checkpoint> [--note]` | Approve a phase (the next one starts) or reject it (a refine round re-runs every step of the phase with the note); also clears inner checkpoints |
| `resume <run-id>` | Continue from the last completed step |
| any `run …` `--dry-run` | Print the agent commands and gates without executing |

## config.yaml (shape, abbreviated)

```yaml
version: 1

project:
  base_branch: main            # PR target (finishing skill: work forks from `next`, PRs go to main)
  fork_from: next
  branch: "flow/{lane}/{slug}"
  worktrees_dir: ../tivacare-worktrees
  docs: {idea: docs/ideas/agentic-rag-customer-support.md, capability_map: docs/specs/CAPABILITY-MAP.md,
         specs: docs/specs, plans: docs/plans, decisions: docs/decisions}
  commit_trailer: "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"

agents:
  claude:
    enabled: true
    command: claude
    base_args: [-p, --output-format, stream-json, --verbose]
    permission_mode: acceptEdits   # tool allow/ask/deny come from .claude/settings*.json, not from here
    models: {plan: opus, build: sonnet, mechanical: haiku, review: opus, debug: opus}  # fallback when a phase sets no model
    effort_arg: [--effort, "{effort}"]   # the level is the agent's choice (see `effort:`), never set here
    max_turns: 80
  codex:
    enabled: false
    command: codex
    base_args: [exec, --json]
    profile: agentflow          # sandbox/approval policy lives in Codex's own config.toml profile
    models: {plan: gpt-5-codex, build: gpt-5-codex, review: gpt-5-codex}
    effort_arg: [-c, "model_reasoning_effort={effort}"]
    skill_loading: read_file   # prompt tells it to read .claude/skills/<skill>/SKILL.md

defaults:
  agent: claude
  step_timeout_min: 45
  fix_attempts: 3              # gate failure → same session resumed with the gate output
  run_budget_usd: 25           # stop + checkpoint when the run's summed cost crosses it
  commit: per_task
  push: true
  pr: draft

gates:                          # names → commands; copied from CONSTRAINTS.md, never weakened here
  floor:        {run: "python3 devtools/floor_guard.py --base origin/main"}
  py_lint:      {run: "uv run ruff check . && uv run ruff format --check .", when_exists: pyproject.toml}
  py_types:     {run: "uv run pyright", when_exists: pyproject.toml}
  py_unit:      {run: "uv run pytest tests/unit", when_exists: tests/unit}
  py_cov_diff:  {run: "uv run diff-cover coverage.xml --fail-under=80", when_exists: coverage.xml}
  web_types:    {run: "pnpm exec tsc --noEmit", cwd: webadmin, when_exists: webadmin/package.json}
  web_lint:     {run: "pnpm biome check .", cwd: webadmin, when_exists: webadmin/package.json}
  web_unit:     {run: "pnpm exec vitest run --coverage", cwd: webadmin, when_exists: webadmin/package.json}
  eval:         {run: "uv run python -m tivacare.eval.cli gate", when_exists: src/tivacare/eval}
gate_sets:
  build:  [floor, py_lint, py_types, web_types, web_lint]          # CONSTRAINTS Lifecycle: Build
  verify: [floor, py_lint, py_types, py_unit, py_cov_diff, web_types, web_lint, web_unit]

guardrails:
  forbidden_paths:              # any diff here fails the step (agent may not touch them in a run)
    - CONSTRAINTS.md
    - devtools/floor_guard.py
    - eval/baseline.json
    - devtools/agentflow/config.yaml
    - .claude/**
  reject_trailers: ["Constraints-Approved-By"]
  user_only_skills: [improve-codebase-architecture, constraint-driven-development,
                     doubt-driven-development, using-git-worktrees, writing-for-agents]

overlays:
  security:
    when_paths: ["src/tivacare/staff_auth/**", "**/identity/**", "**/attachments/**",
                 "**/redaction/**", "**/widget/**", "src/tivacare/knowledge/retrieval/**"]
    when_keywords: [IS4, Entra, customer_code, "visibility = public", attachment, redaction]
    add_skills: {plan: security-and-hardening, review: security-and-hardening}
    review_agent: security-auditor
    checkpoint_before_build: offer_doubt_driven     # human decides; never auto-runs the skill

lanes:
  spec:                                                                # Specify stage (see Conditional steps)
    steps:
      - {id: draft, skill: spec-driven-development, role: plan, prompt: spec.md,
         output: "docs/specs/SPEC-{module}.md", unless: spec_exists}
      - {id: contracts, skill: api-and-interface-design, role: plan, when: spec_defines_routes}
      - id: grill_open_questions
        when: spec_has_open_questions
        repeat: {until: {not: spec_has_open_questions}, max_rounds: 2}  # then HITL
        steps:
          - {id: grill, skill: grilling, role: plan, prompt: grill_spec_questions.md,
             output: "runs/{run}/questions.md"}
          - {id: answers, type: checkpoint, wait_for: answered_file}
          - {id: apply, skill: domain-modeling, role: plan, prompt: spec_apply.md}
      # the runner adds the phase approval `spec_approved` after this phase
  feature:
    steps:
      - {id: spec_ready, type: lane, lane: spec, unless: spec_is_approved}   # runs the spec lane first
      - {id: plan, skill: writing-plans, role: plan, prompt: plan.md,
         output: "docs/plans/{date}-{module}-{slug}.md", commit: true}
      # the runner adds `plan_approved` after the plan phase (and an approval after every phase)
      - {id: doubt_offer, type: checkpoint, when: security_path}       # offer /doubt-driven-development
      - {id: build, foreach: plan_task, skill: test-driven-development, role: build,
         prompt: task.md, gates: build, fix_loop: true,
         review: {skill: requesting-code-review, role: review, prompt: task_review.md},
         commit: per_task}                                             # ticks the plan's - [ ] boxes
      - {id: verify, skill: verification-before-completion, role: build, gates: verify, fix_loop: true}
      - {id: branch_review, skill: requesting-code-review, role: review, prompt: branch_review.md}
      - {id: finish, type: finish}                                      # push + gh pr create --draft
  bug:
    steps:
      - {id: debug, skill: systematic-debugging, role: debug, prompt: debug.md}
      - {id: regression_test, skill: test-driven-development, role: build, expect: red}  # new test must FAIL
      - {id: fix, role: build, gates: verify, expect: green, fix_loop: true, commit: true}
      - {id: review, skill: requesting-code-review, role: review}
      - {id: finish, type: finish}
  eval:
    applies_to: [src/tivacare/knowledge/retrieval/**, src/tivacare/knowledge/tables/**,
                 src/tivacare/crm/model_fields.py, src/tivacare/llm_gateway/models.toml,
                 src/tivacare/agent/policy.py, src/tivacare/agent/prompts/**]
    steps:
      - {id: change, role: build, prompt: eval_change.md, tdd: false}
      - {id: eval_gate, gates: [floor, eval], fix_loop: true, on_fail: block}   # never lowers baseline
      - {id: review, skill: requesting-code-review, role: review}
      - {id: finish, type: finish}
  design:
    steps:
      - {id: grill, skill: grilling, role: plan, prompt: grill_questions.md,
         output: "runs/{run}/questions.md"}                # questions + recommended answers
      - {id: answers, type: checkpoint, wait_for: answered_file}
      - {id: apply, skill: domain-modeling, role: plan, prompt: design_apply.md,
         checks: [decisions_log_row, open_question_removed, adr_index_updated]}
      - {id: commit, type: commit, message: "Decide {topic} in agentic RAG idea doc"}
      # design lane commits only; it never pushes or opens a PR (doc work lands on `next`)
```

## Conditional and optional steps

Most steps aren't for every run: grilling only when a spec still has open questions, `api-and-interface-design` only when a spec defines routes, the security overlay only on security paths, `executing-plans` vs `subagent-driven-development` by plan size. Config expresses this with **named conditions** and three step keys.

### Step keys

| Key | Meaning |
|---|---|
| `when: <cond>` | Run the step only if the condition is true; otherwise it is **skipped** (logged with the reason) |
| `unless: <cond>` | The inverse |
| `repeat: {until: <cond>, max_rounds: N}` | Re-run a step group until the condition holds; after N rounds → HITL stop |
| `next: [{when: <cond>, goto: <step-id>}, …]` | Branch after a step (first match wins; default: the following step) |
| `type: lane, lane: <name>` | Run another lane as a sub-lane (feature → spec when the spec isn't approved) |

`<cond>` is a condition name, or a composition: `{all: [a, b]}`, `{any: [a, b]}`, `{not: a}`.

### Named conditions (`conditions:` block)

Conditions are evaluated **by the runner, deterministically, from files and git**, before the step starts, so whether a step runs never depends on a model's mood. Each uses a built-in check with config-tuned parameters, because the docs aren't uniform (e.g. SPEC-platform's Open Questions has "Open now: None", a deferred "For the AWS stage" list, and a "Resolved" list; SPEC-settings says "None for this module." then "Resolved …"):

```yaml
conditions:
  spec_exists:            {check: file_exists, path: "docs/specs/SPEC-{module}.md"}
  spec_has_open_questions:
    check: section_items                      # counts open items in one section
    file: "docs/specs/SPEC-{module}.md"
    section: "^## Open Questions"
    stop_at: ['^\*\*Resolved', '^\*\*For the AWS stage']   # deferred/resolved lists don't count
    none_patterns: ['^(- )?None\b', '^None for this module']
    min: 1
  spec_is_approved:
    check: status_line
    file: "docs/specs/SPEC-{module}.md"
    approved: '\*\*Status: approved'
    pending: 'pending approval'               # a later "Amended …, pending approval" wins
    on_unknown: ask_human                     # no status line → the human says
  spec_defines_routes:    {check: grep, file: "docs/specs/SPEC-{module}.md",
                           pattern: '\b(GET|POST|PUT|PATCH|DELETE) /|WebSocket'}
  security_path:          {any: [{check: changed_paths, globs: "@overlays.security.when_paths"},
                                 {check: grep, file: "{plan}", pattern: "@overlays.security.when_keywords"}]}
  eval_paths_touched:     {check: changed_paths, globs: "@lanes.eval.applies_to"}
  plan_is_small:          {check: plan_tasks, file: "{plan}", max: 3}      # → executing-plans
  idea_doc_question_open: {check: section_items, file: "{idea}", section: "^## Open Questions",
                           match: "{topic}"}
  deps_merged:            {check: modules_done, from: capability_map}
  custom_example:         {check: command, run: "test -f webadmin/package.json"}   # escape hatch: exit 0 = true
```

Built-in checks: `file_exists`, `grep`, `section_items`, `status_line`, `changed_paths`, `plan_tasks`, `plan_unchecked`, `modules_done`, `gate_passed`, `branch_merged`, `result_signal` (a value the previous step's agent put in `result.json`, e.g. `signals.needs_contracts`), `command`. Each returns `true | false | unknown`.

### When a condition can't be decided

`on_unknown` per condition: `ask_human` (default; HITL inbox entry "Spec has no status line. Treat as approved?"), `agent` (a cheap `mechanical`-role call that answers one yes/no question in `result.json`, with its evidence; logged as agent-decided), `true`, or `false`. Agent judgement is only ever the fallback for an undecidable file check, never the first choice.

### The worked example: grill a spec only if it has open questions

`flow run spec --module settings`:
1. `draft` skipped (`spec_exists` = true).
2. `contracts` runs (`spec_defines_routes` = true).
3. `grill_open_questions` skipped: `spec_has_open_questions` = false ("None for this module.", items after `**Resolved` ignored).
4. `spec_approved` waits for the human, even though the spec's status line already says approved: `contracts` ran and may have changed the spec, and every phase that ran needs an approval. Approve and the lane is done. Reject with a note and both `contracts` and the spec's other steps are refined (the skipped ones re-check their conditions).

For a new spec whose draft leaves Q1–Q3 open: step 3 runs, `grilling` turns Q1–Q3 into a questions file with recommended answers (HITL), `domain-modeling` applies the answers into the spec (and the idea doc / ADRs when product-level), and the group repeats until no open items remain or 2 rounds pass. Then `spec_approved` waits for the human.

### Seeing the decisions

- `flow explain <lane> --module <id>` evaluates every condition now and prints the step list as it would run: `✓ run`, `– skip (spec_has_open_questions = false: 0 open items, 9 resolved)`, `? ask (spec_is_approved = unknown)`. No agent calls.
- `flow status` shows each skipped step and its reason; the reasons go into the PR body.
- `flow validate` fails on a `when`/`unless`/`until` that names an undefined condition, a `goto` to a missing step, or a `repeat` without `max_rounds`.

The YAML is the whole workflow contract: adding a lane, swapping a phase's model, narrowing the effort range, adding a gate when the platform skeleton lands, or moving review to Codex is a config edit, not a code change. **Tool permissions are not in it** — see below.

## Human in the loop (HITL)

Every step can stop and hand the decision to a human. The run never guesses through a product decision, and the answer goes back into the *same* agent session, so nothing is lost.

### Three ways a step reaches a human

| Kind | Raised by | When |
|---|---|---|
| **Planned checkpoint** | the runner (phase approvals) and config (`type: checkpoint`) | The approval at the end of every phase that ran; design-lane answers; security overlay's "run `/doubt-driven-development`?" offer; PR merge (on GitHub, outside the runner) |
| **Agent can't decide** | the agent, via `result.json` `status: needs_human` | A choice the spec, idea doc, ADRs and CLAUDE.md don't settle (a spec gap, two readings of a requirement, a plan task that conflicts with the code, a review finding it disagrees with) |
| **Runner escalation** | the runner | Fix attempts exhausted; a guard tripped (forbidden path, agent commit, user-only skill); eval regression; a bug-lane regression test that passes before the fix; review findings still open after the one re-review; step timeout; run budget crossed; agent `blocked` |

Every prompt template carries the same rule (from CAPABILITY-MAP: "gaps go into Open Questions until the idea doc decides them"): *if the decision is not settled by the docs, stop and ask; don't pick one and continue.* The agent then writes:

```json
{"status": "needs_human",
 "summary": "Task 4 needs the MISA token cache TTL; SPEC-crm M5 says 'once per conversation' but not across conversations.",
 "questions": [{"id": "q1", "question": "Cache the MISA token across conversations?",
                "options": [{"key": "A", "label": "Per process until expiry", "recommended": true},
                            {"key": "B", "label": "Per conversation (re-auth each time)"}],
                "evidence": ["docs/specs/SPEC-crm.md#M5", "src/tivacare/crm/client.py:42"],
                "kind": "spec_gap"}],
 "can_continue_without": false}
```

`kind` is `spec_gap | ambiguity | conflict | review_dispute | other`; `can_continue_without: true` lets the runner park the question and move to the next *independent* task (the plan marks dependencies), so one question doesn't stall a whole module.

### What happens when a step needs a human

1. The runner writes `runs/<run>/inbox/<step>.md` (summary, questions with options and the recommended one, evidence links, the gate/review output if the runner raised it, and the exact commands to answer), sets the run to `waiting_human`, and notifies (config `hitl.notify`).
2. In `queue` mode only that module's run pauses; other ready modules keep building.
3. The human answers in one of three ways:
   - `flow answer <run>`: walks the questions in the terminal (pick an option, or type free text); same as editing the inbox file's `Answer:` lines.
   - `flow attach <run>`: opens the paused agent's session interactively (`claude --resume <session_id>` in its worktree) to talk it through; on exit, `flow resume` continues the step.
   - `flow approve <run> <checkpoint>` / `flow reject <run> <checkpoint> --note "…"` for phase approvals and planned checkpoints (a rejected phase runs a refine round: every step of it again, with the note; see Refine round).
4. `flow resume <run>` resumes the same session with the answers appended, re-runs the step's gates, and goes on.
5. **Decisions don't stay in run logs.** If an answered question is `spec_gap` (or the human marks it `--record`), the runner queues a design-lane step that writes the decision into the spec (and idea doc / ADR when it is product-level), committed as its own `Decide …` commit, before the build continues. Answers of kind `ambiguity` / `review_dispute` go into the task's commit message body only.
6. Reminder after `hitl.remind_after_h`; a run never times out into an automatic decision.

### config.yaml: `hitl`

```yaml
hitl:
  notify:
    - {type: terminal_bell}
    - {type: macos, title: "agentflow: {run} needs you"}       # osascript display notification
    - {type: webhook, url_env: AGENTFLOW_WEBHOOK_URL, enabled: false}  # Slack/Teams/ntfy later
  remind_after_h: 4
  record_decisions: {spec_gap: design_lane, ambiguity: commit_body, review_dispute: commit_body}
  park_independent: true          # honour can_continue_without
```

The runner always raises its own stops (fix attempts exhausted, guard violation, eval regression, red test passing, unresolved review, budget exceeded, step timeout). An earlier sketch listed them under `hitl.escalate_on`, but nothing read that key, so it was removed from `config.yaml`.

### Where HITL sits in each lane

| Lane | Step | Human is asked when |
|---|---|---|
| feature | `spec_ready` | Spec not approved → stop, `Next: spec-driven-development` (specs are human-approved) |
| any | end of every phase that ran | Always: approve (the next phase starts) or reject with a note (refine round) |
| feature | `plan` → `plan_approved` | Always: approve / reject with a note; a reject re-runs the plan phase's steps |
| feature | security overlay | Always before Build on a security path: run `/doubt-driven-development` yourself or skip |
| feature | `build` (per task) | Agent `needs_human`; gates fail after 3 fixes; guard trips; review findings unresolved |
| feature | `verify` / `branch_review` | Gates fail after fixes; branch review raises a blocking finding the agent disputes |
| feature | `finish` | Never inside the runner: the draft PR is the human's review and merge |
| bug | `debug` | Root cause unclear, or the "bug" may be intended behaviour per the spec |
| bug | `regression_test` | The new test passes before any fix (doesn't reproduce) |
| eval | `eval_gate` | Any regression past `eval/baseline.json` limits: never auto-fixed by lowering the baseline (ADR-031 needs a human trailer) |
| design | `grill` → `answers` | Always: the questions file is the human's to answer |
| design | `apply` | Doc checks fail (Decisions Log row, Open Questions removal, ADR index) after fixes |
| any | any | Budget crossed, timeout, agent `blocked` |

## Tool permissions: `.claude/settings.json` / `.claude/settings.local.json`

The runner passes no `--allowedTools` / `--disallowedTools`. Headless `claude -p` reads the same settings files as interactive sessions, and in `-p` mode any call that would need a prompt (an `ask` rule or an unlisted tool) is denied instead of prompting. So one set of rules serves both:

- **`.claude/settings.json` (committed, shared by every developer and future CI):** `permissions.allow` gets the gate commands of `config.yaml` and nothing wider (`Bash(uv run ruff check *)`, `Bash(uv run pyright *)`, `Bash(uv run pytest *)`, `Bash(pnpm exec vitest *)`, … — never `Bash(uv run *)`, `Bash(uvx *)` or `Bash(pnpm *)`, which would match `uv run git push` or `pnpm dlx`), `Bash(python3 devtools/floor_guard.py *)`, `Bash(git diff *)`, `Bash(git status *)`, `Bash(git log *)`, `Bash(git show *)`. `permissions.ask` gets `Bash(git push*)` and `Bash(gh pr *)`: an interactive session still asks first (matches "push/PR only when asked"), and a headless agent is refused — only the runner pushes and opens the draft PR. `permissions.deny` gets what nobody's agent should do: `Bash(git push --force*)`, `Bash(git reset --hard*)`, `Read(./.env)`, `Read(./.env.*)`.
- **`.claude/settings.local.json` (git-ignored, per developer):** keeps today's personal entries (`git add`, `git commit`, `WebSearch`, `skillOverrides`, `enabledPlugins`). A developer can widen or narrow their own runs here without touching the shared file.
- `git commit` stays allowed (it is in the local file today and changing that would alter interactive sessions). The runner enforces "the runner commits" itself: if `HEAD` moved during an agent step, the step fails.
- Path rules like "never edit `CONSTRAINTS.md` / `eval/baseline.json`" stay as **runner diff guards** (`guardrails.forbidden_paths`), not `deny` rules, because humans legitimately edit those files interactively (`/constraint-driven-development`, ADR-031 baseline approvals).
- Codex has no Claude settings; its sandbox and approval policy go in a Codex `config.toml` profile (`agentflow`: `sandbox_mode = "workspace-write"`, `approval_policy = "never"`), and config.yaml only names the profile.
- `flow validate` reads the merged settings and warns when a gate's command or a step's needs aren't covered by an `allow` rule (e.g. a new `pnpm` gate), so a run fails at validation, not 40 minutes in.
- Precedence (deny > ask > allow across files) and the `-p` denial behaviour get checked against the Claude Code docs/`claude --help` before writing the rules.

## How a step runs (lanes.py)

1. **Prompt** = template from `prompts/` + context the runner gathers (spec path, plan task text, gate output, previous review). It names the skill ("Use the `writing-plans` skill"), opens with the effort check (size the task and ask for low or high if medium doesn't fit; left out once the step is sized), forbids commits/pushes, and ends by asking the agent to write `runs/<id>/<step>.result.json` (`status: done|blocked|needs_human|effort_change|unchanged`, `summary`, `next`, plus `effort` and `reason` on a switch; `unchanged` is valid only in a refine round).
2. **Agent call** via the adapter. Claude: `claude -p <prompt> --model <resolved model> --effort <step's level> --permission-mode acceptEdits --max-turns …` in the worktree (see Model and effort per step; an `effort_change` from the sizing check restarts the step in a new session at the requested level, and every `--resume` passes the same level) (tool rules from `.claude/settings*.json`); the stream-json result gives `session_id` and `total_cost_usd`, recorded in state. Project skills load headlessly from `.claude/skills/`. Codex: `codex exec --json -m … -c model_reasoning_effort=… --cd <worktree>`, with skill files referenced by path.
3. **Guards** on `git diff`: forbidden paths, `HEAD` moved by the agent, rejected trailers, user-only skill names in the agent's transcript tool calls → step fails, no retry. Permission denials reported in the stream-json result are logged so a missing `allow` rule is easy to spot.
4. **Gates**: run the configured set; missing `when_exists` targets are reported as *skipped*, never as passed.
5. **Fix loop**: on gate failure, resume the same session (`--resume <session_id>`) with the failing output, up to `fix_attempts`; then a HITL stop (see Human in the loop).
6. **Review** (per task / per branch): a fresh reviewer session with the diff; findings go back to the implementer session once (`receiving-code-review`), then gates re-run.
7. **Commit** (runner, not agent): `Task N: <title>` + trailer; plan checkboxes for that task ticked in the same commit.
8. **Finish**: push the branch, `gh pr create --draft --base main` with the plan link, gate summary and spend; PR body ends with the Claude Code attribution line.
9. `agent: blocked` or `needs_human`, a budget hit, or a checkpoint → HITL inbox file + notification, state saved, `status` shows what is needed, `answer`/`attach`/`approve` then `resume` continues.

`queue` reads CAPABILITY-MAP's build order into a DAG, treats a module as done when its plan is fully checked and its PR is merged (`gh pr view`), and runs ready modules in parallel worktrees up to `max_parallel`. Each module's run stops at every one of its phase approvals, so humans gate each module phase by phase, while the other ready modules keep going.

## Files to change outside `devtools/agentflow/`

- `CLAUDE.md` → Agent workflow: one paragraph "Automated runs": `devtools/agentflow/config.yaml` is the durable permission for commits, pushes and draft PRs **inside `agentflow` runs only**; interactive sessions keep "commit only when asked". Lanes and stage owners are unchanged.
- `docs/agent-orchestration-guide-v3.md`: new §11 "Automated runs" (what the runner adds, how to read `status`, how to change config). Sections 1–10 untouched.
- `.claude/settings.json`: the shared `permissions.allow` / `ask` / `deny` rules above (today it only holds `enabledPlugins: {}`).
- `.gitignore`: `devtools/agentflow/runs/`.
- `AGENTS.md` (root): short pointer for Codex to CLAUDE.md and `.claude/skills/`.
- No change to skills, `next-step`, `VENDORED.md`, or `CONSTRAINTS.md`.

## Build order (one testable slice + one commit each; lane: new feature, so the first step is saving this as `docs/plans/2026-10-28-agentflow.md` via `writing-plans`)

1. `config.py` + `config.yaml` + `validate` (schema errors name the YAML path).
2. `agents.py`: Claude + Codex adapters, `--dry-run`, stream-json parsing (session id, cost, token usage), auto-compact env override, rollover with `progress.md`, the `effort_arg` flag on every call including resumes (already checked: `--resume` accepts `--effort`), and stdin closed (`< /dev/null`), since `claude -p` otherwise waits 3 s for piped input. Check the remaining flags with `claude --help` (v2.1.312) and `codex exec --help` (0.153.4) when writing it.
3. `gates.py` + `guards.py`, the `.claude/settings.json` permission rules, and `validate`'s settings-coverage check.
4. `state.py` + `hitl.py`: run dirs, `result.json` schema, inbox files, notifications, `status`, `inbox`, `answer`, `attach`, `approve`/`reject`, `resume`, parking independent tasks.
5. `conditions.py`: built-in checks, composition, `on_unknown`, `when`/`unless`/`repeat`/`next`/sub-lanes in the executor, `explain`.
6. `pipelines`: phase loop, `start_at: auto`, `skip_if`, `--from/--to`, `handoff.md`, footer cross-check, phase approvals and refine rounds (every step re-run with the note, `unchanged` results, `Refine …` commits), per-phase model resolution and `--model` overrides, and the agent's effort choice (the sizing check, `effort_change` restarts, the `allowed` range, the same level on every resume).
7. `sources.py` + spec lane + feature lane (plan parsing, per-task loop, fix loop, review, commit, finish).
8. Bug lane (red/green expectations) + eval lane + security overlay.
9. Design lane (questions file → answers → apply → doc checks → commit).
10. `next` + `queue` (DAG, worktrees, parallelism, chaining modules on merge).
11. `events.py` is built with task 4 (state); here: `status --watch`, `agents --watch`, `pause`/`cancel`/`retry`, and `flow dashboard` (API, SSE, Kanban page, Agents view, actions, token).
12. Docs: CLAUDE.md paragraph, guide §11, AGENTS.md, .gitignore.

## Verification

- `uv run --with pyyaml --with pytest pytest devtools/agentflow/tests`: tests use a **fake agent** (config `command:` pointed at a stub script that edits files and writes result.json), covering: fix loop resumes the session and stops after N attempts; a forbidden-path edit fails the step; a `Constraints-Approved-By` trailer is rejected; skipped gates show up as skipped; the bug lane rejects a regression test that passes before the fix; resume after a checkpoint continues at the right task; plan checkboxes get ticked; a fake `needs_human` result writes the inbox file, notifies, and `answer` + `resume` passes the answer into the resumed session; `can_continue_without: true` parks the task and the next independent one runs; a `spec_gap` answer queues the design-lane step before the build continues.
- `python3 devtools/floor_guard.py` clean on the branch.
- Condition tests against the **real specs** (read-only): `spec_has_open_questions` is false for SPEC-settings, SPEC-staff-auth and SPEC-platform (its AWS-stage list is deferred), and true for a fixture spec with "Q1 …" under Open Questions; `spec_is_approved` handles a later "pending approval" amendment; a missing status line gives `unknown` → inbox entry.
- Pipeline tests with the fake agent: a full `feature` run stops at `<phase>_approved` after every phase that ran and starts nothing until `approve`; a skipped phase raises no approval; `reject --note` re-runs every step of that phase (each resuming its own session with the note, at its recorded effort), commits the changes as new `Refine …` commits, and raises the approval again; a step answering `unchanged` is listed at the next approval; in `build` every task is refined; `validate` rejects config that tries to skip a phase approval; the runner starts each phase in a new session and passes `handoff.md`; `start_at: auto` on a repo with an unchecked plan starts at `build`; `--from plan --to test` stops after test; a skill footer that disagrees with the config route is logged and ignored.
- Model and effort tests with the fake agent: a `build` task's implementer gets `--model sonnet` and its reviewer `--model opus`; a phase with no `model:` falls back to the role model; `--model plan=fable` changes only the plan phase; every session starts with `--effort medium`; a fake `effort_change` to high from the sizing check starts a new session with `--effort high` and no sizing check; that step's fix-loop, review and HITL resumes and its post-rollover session all get `--effort high` in their argv, read from `state.json` (never omitted on a resume); an `effort_change` after work has started is refused and the session goes on at its level; a request for `xhigh` or `max` is refused and the step runs at medium; the next step starts at medium again; every switch appears as an `effort_changed` event with the agent's reason; `validate` rejects a model for a disabled agent.
- Context tests with the fake agent emitting usage: every `claude -p` gets the auto-compact env set to 60; a session reporting 81% usage is ended and the step continues in a new session whose prompt includes `progress.md`; a third rollover becomes a HITL stop; a phase switch always starts a new session id.
- Tracking tests: every step transition appends the expected events; `state.json` matches a replay of `events.jsonl`; a killed runner shows `stalled` and `resume` continues; dashboard API returns the cards for fixture runs, rejects requests without the token, `/api/agents` returns one row per live agent session across two fixture runs with its phase, step, role, model and effort (an `effort_changed` event updates the row's level and marks it `↑`, a fix-loop resume keeps the same row, and `agent_exited` or a dead PID moves it to exited), an Approve call clears the checkpoint exactly like `flow approve`, and a Reject call starts the same refine round as `flow reject`.
- Manual: start two fake-agent runs, open `flow dashboard`, watch cards move across phase columns live, switch to Agents and see both agents with the phase › step, model and effort each was started with, answer a Needs-human item from the board, and see the run resume.
- `flow explain feature --module crm` lists brainstorm and spec as skipped with reasons and starts at design/plan.
- `flow explain spec --module settings` prints contracts ✓, grilling skipped with its reason, and `spec_approved` required (contracts ran).
- `uv run devtools/agentflow/flow.py validate` and `flow next` (should say design-doc / feature-plan for the current repo state).
- `flow run feature --module platform --dry-run`: prints the plan → checkpoint → per-task commands with no side effects.
- Real smoke test 1: `flow run design --topic "<a small open question from the idea doc>"`, answer the file, check the idea doc's Decisions Log/Open Questions edit and the commit message.
- Real smoke test 2: `flow run feature --module platform` up to `plan_approved`, reject it once with a note and check that every plan-phase step re-ran and a `Refine …` commit landed, then approve; let it build the first 1–2 tasks, check commits, gate logs and spend in `status`, then approve `build`, `test` and `review` in turn to reach the draft PR, and check that no phase started before its approval.
