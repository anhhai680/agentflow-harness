# Proposal: agentflow throughput — from one serial agent to a scheduled team

Status: proposed (2026-11-15)

Related: `docs/agentflow-harness-proposal.md` (§3.3 conductor, §3.11 cost governance, §6b effort sizing and "L2, parallel sessions"), `docs/specs/agentflow-issue-dependencies-proposal.md` (the Backlog's DAG), `docs/specs/agentflow-verify-review-proposal.md`.

## Context

agentflow is meant to run with minimal human involvement. Today a real feature, from `define` to a PR that is ready for review, takes **8–9 hours of wall time**, and the developer has to watch it the whole way. A run stops, raises an inbox item, and waits. Nobody can leave a feature running in the morning and come back to a finished PR. That defeats the purpose of the harness.

The reason isn't that the agents are slow. One agent works at a time, and the run waits on gates, people and CI in turn:

1. **One agent at a time.** Every step is one `claude -p` session, and the runner walks `phases:` one step at a time. A plan of 23 tasks becomes 23 implement sessions and 23 review sessions, one after the other. No two sessions in a run ever overlap.
2. **The run blocks on people.** Almost every stop (`blocked`, a resolver's stop, `guard_violation`, `blocked_repeat`, `ci_failed`, a checkpoint) halts the *whole* run, including work that doesn't depend on the stop. Parking exists (`hitl.park_independent`, with D12 `Depends on:`), but it is opt-in, and it covers only a `needs_human` result that the agent itself marked `can_continue_without`.
3. **Fixed depth.** Every feature walks the same route at the same depth: grilling, up to 3 doubt rounds on the spec, up to 3 on the plan, per-task review, verify, branch review and ship. A one-module change pays the same ceremony as an epic.
4. **The full test suite runs again and again.** The `verify` gate set (13 gates, including a ~10-minute integration suite) runs again in `verify/fix_findings`, `review/fix_findings`, `ship/finish` and every `ship/ci/fix-N`. Then CI runs the same suite once more. The gates inside a set run one after another.

This proposal keeps the core rule of `agentflow-harness-proposal.md`, "the model proposes, the harness disposes". It amends §6b item 1 in one narrow place: a build session may spawn sub-agents, but only for the plan tasks the runner put in its wave (§4.4). There is still no long-lived orchestrator. The **runner** still starts every top-level session, attributes, gates, reviews and commits the work, and pushes. What changes is that the runner becomes a **scheduler**:
- it runs the tasks the plan marks as parallel in one session that fans them out to sub-agents;
- it parks a stop instead of blocking on it;
- it sizes the route to the task;
- it runs each expensive gate once, on the tree that ships.

---

## 1. Evidence: where the hours go

Method: `events.jsonl` of every feature and bug run of the `customers-support-agentic-ai` pack on this machine (16 runs with activity, 2026-11-07 → 2026-11-15). The script reads only events:
- agent time is `agent_started` → `agent_exited`;
- gate time is the sum of `gate_result.duration_s`;
- human wait is `hitl_raised`/`phase_approval_raised` → its answer, approval, rejection or cancel.

### 1.1 Wall time by bucket (16 runs, 99.3 h of wall time)

| Bucket | Hours | Share | What it is |
|---|---:|---:|---|
| Waiting for a human | 32.1 | **32 %** | the run is stopped on an inbox item |
| Agent sessions | 24.2 | 24 % | one `claude -p` session at a time |
| Gates | 17.2 | 17 % | one gate at a time; `backend_integration_tests` alone is 10.4 h |
| Idle / CI / overhead | ~25.8 | 26 % | CI polling (up to 7 h in one run), runner stopped, gaps between steps |

- The median run is 5.4 h.
- The 7 runs with a real plan (9–24 tasks) average **9.3 h**: 24.0, 13.2, 8.5, 6.5, 5.8, 5.0 and 1.7 h. That matches the 8–9 h developers feel.
- Total spend was $489.

**Only about a quarter of a run's wall time is agents working.** Even an infinitely fast agent would leave a 9-hour run at about 7 hours. The biggest wins are in the other three buckets.

### 1.2 Human waits by kind

| Kind | Count | Hours | Typical cause (from the `summary` of the items) |
|---|---:|---:|---|
| `needs_human` | 20 | 9.7 | a guard false positive (`floor_guard` read a renamed assertion as removed) blocked task 9 of issue-81 for **9.3 h** while tasks 10–23 sat idle |
| `blocked` | 20 | 8.5 | a test pinned to the wall clock; a missing baseline file |
| `blocked_repeat` | 5 | 6.1 | the environment: `pnpm` broken under Node 21. The same question was raised 3 times in 5 h, and approving it could not fix it |
| `ci_failed` | 2 | 3.5 | "CI did not settle within 45 minutes: 5 checks still pending" |
| `checkpoint` | 3 | 1.7 | grilling answers |
| `guard_violation` | 13 | 1.2 | |
| `phase_approval` | 14 | 1.1 | spec approval |

None of these stops needed the *whole run* to stop. Most of them weren't decisions at all: environment faults, CI that hadn't finished, and the same question asked again.

### 1.3 One run, step by step (issue-81, 23 tasks, still running at 13.2 h)

```
define  draft 8.6m · grill r1+r2 1.7m · spec_review→reconcile ×2 + review 18.8m      ≈ 45 min
plan    plan 25.7m · plan_review → plan ×2 + reviews 19.9m                            ≈ 45 min
build   task-1 … task-8 implement+review, strictly one after another                  ≈ 60 min
        task-9 → floor_guard stop raised 00:44 local → answered 10:03 local            9.3 h
                → 1 h of back-and-forth: the human's override commit tripped the
                  agent_commit guard 3×, resolve-2 failed ×3 (≈ 30 min each)          ≈ 1.2 h
        tasks 10–23: not started overnight
```

Tasks 10–23 sat idle overnight for two reasons:
- this pack doesn't set `hitl.park_independent`;
- the stop was a guard stop, and parking covers only `needs_human` + `can_continue_without`.

After the answer came, the human needed 8 more touches. A project guard that only a human may override (`Constraints-Approved-By`) has no first-class path through the runner.

Across all runs, `build` is **71 %** of agent time:
- 12.8 h in implement sessions (243 of them);
- 2.8 h in per-task reviews (154);
- 1.6 h in resolvers (34).

Define and plan together are 4.4 h. The run's `subagent_started` count is 0, so no work was split inside a session either.

### 1.4 The full suite runs many times

For each run, the steps that ran `backend_integration_tests` (~6–13 min each). CI runs the suite again after these:

| Run | Steps that ran the full suite |
|---|---|
| feature-eval | verify/verify, verify/fix_findings, review/fix_findings, ship/finish, ship/ci/fix-1 |
| issue-87 | verify/verify, review/fix_findings, ship/ci/fix-1, ship/ci/fix-2 |
| feature-settings | verify/verify, ship/ci/fix-1, ship/ci/fix-2, ship/ci/fix-3 |

---

## 2. Goals and non-goals

**Goals** (measured with the KPIs in §6):

| KPI | Today | Target |
|---|---|---|
| Lead time, `define` start → PR ready, feature with ≥ 9 tasks | 9.3 h mean | **≤ 3 h** median |
| Lead time, small feature (≤ 3 tasks) | 1.5–3 h | **≤ 45 min** |
| Synchronous human touches per run | 3–14 | **≤ 1**: spec approval, and it doesn't block planning |
| Share of wall time the run is fully stopped on a human | 32 % | **≤ 5 %** |
| Full-suite runs per feature, local | 2–5 | **1**, on the final tree |
| Features in flight per developer | 1, sometimes 2 | **3**, with isolated resources |
| Conformance SLO (`agentflow scorecard`) | baseline | no regression |
| Cost per feature | $5–104 | within +20 % (parallelism shouldn't add tokens; it adds overlap) |

**Non-goals:**
- A free-form LLM orchestrator that decides what to spawn. §6b item 1 rejected it for good reasons: the runner couldn't see, meter, resume or audit those sessions. §4.4 allows a much narrower thing: a **wave session** that spawns one sub-agent per task the runner handed it. The session is short-lived and its sub-agents are visible as events. Everything they produce is gated, reviewed and committed by the runner.
- Removing any engine-floor obligation: `gates:verify` on the final tree, the reviewed obligation, the runner as the only committer, and `.agentflow/**` forbidden.
- Auto-merge. The run still ends with a PR that is ready for review.

---

## 3. What the reference harnesses do

| Harness | Pattern | What we take | What we leave |
|---|---|---|---|
| [awslabs/aidlc-workflows](https://github.com/awslabs/aidlc-workflows) | A deterministic engine emits one typed directive at a time, and the LLM conductor executes it ([01-architecture.md](https://github.com/awslabs/aidlc-workflows/blob/main/docs/reference/01-architecture.md)). **Scopes** are EXECUTE/SKIP grids: Express runs 7 of 30 stages, Bugfix 6 ([workflow-profiles.md](https://github.com/awslabs/aidlc-workflows/blob/main/docs/guide/workflow-profiles.md)). An adaptive composer scores the request and builds a "minimum viable workflow", and can re-estimate mid-flight ([composer agent](https://github.com/awslabs/aidlc-workflows/blob/main/core/agents/aidlc-composer-agent.md)). Units of work carry `depends_on`, which compiles into a DAG of batches. Each unit runs in its own git worktree with a deterministic referee, and merges are serialised and re-verified ([construction and swarm](https://github.com/awslabs/aidlc-workflows/blob/main/docs/harness-engineering/08-construction-and-swarm.md), [swarm protocol](https://github.com/awslabs/aidlc-workflows/blob/main/core/aidlc-common/protocols/stage-protocol-swarm.md)). There is one autonomy grant up front, plan approval is grouped, and the verification command is recorded once ([construction protocol](https://github.com/awslabs/aidlc-workflows/blob/main/core/aidlc-common/protocols/stage-protocol-construction.md)). Failures follow a bounded ladder: 2 fixes in the stage, ≤ 3 targeted loop-backs on the failing unit only, then a human ([build-and-test](https://github.com/awslabs/aidlc-workflows/blob/main/core/aidlc-common/stages/construction/build-and-test.md), [loopback](https://github.com/awslabs/aidlc-workflows/blob/main/core/aidlc-common/protocols/stage-protocol-loopback.md)). Reviewers run on Sonnet at medium effort, "~half the wall-clock … with no verdict/finding quality loss" ([aidlc-tiers.ts](https://github.com/awslabs/aidlc-workflows/blob/main/core/tools/aidlc-tiers.ts)). | Route profiles (§4.3); a DAG of plan tasks run in worktrees with a referee and a merge train (§4.4); grouped approval; the failure ladder; review tiering | The 33-stage ceremony; letting the LLM conductor dispatch |
| [deepseek-ai/dsh-superagent-prototype](https://github.com/deepseek-ai/dsh-superagent-prototype) | "Take as little human attention as possible"; stop "by impact, not by effort" ([philosophy](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/docs/000-philosophy.md)). A worker pauses only for breaking callers, a changed module responsibility, a multi-module redesign or repeated failure ([scope.md](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/prompts/team/scope.md)). It asks **one** structured question and keeps doing the work that is clear ([handoff-worker.md](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/prompts/handoff-worker.md)). Autonomy ramps off → observe → auto ([authorization](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/docs/002-authorization.md)). There are 1–16 parallel workers, one worktree per ticket and a reserved human slot ([execution](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/docs/003-execution.md), [policy.ts](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/src/policy.ts)). Long commands go to the background after 5 s and wake the worker when they finish; nothing polls ([cordis.patch.yml](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/cordis.patch.yml)). | Non-blocking stops, parking and an impact-based stop policy (§4.1); an observe → auto ramp for each approval (§4.1.5); worker slots (§4.6); wake-on-completion for CI (§4.5.5) | One unbounded worker turn per feature. agentflow's step evidence and conformance audit are worth more than that |
| [google-research/harness2](https://github.com/google-research/harness2) | A research framework, not a coding pipeline. Automated gates validate, retry once with the reason, then fall back to the last good state ([recursion.py](https://github.com/google-research/harness2/blob/main/harness2/core/recursion.py)). "Keep base" is a legal answer, so a reviewer can no-op cheaply. Bounded fan-out (`PHASE_WIDTH=2`). Content-addressed completion markers make re-running the same command a cheap resume ([runmark.py](https://github.com/google-research/harness2/blob/main/harness2/core/runmark.py)). Preflight health checks run "before money is spent". There are per-role model channels ([spec.py](https://github.com/google-research/harness2/blob/main/harness2/core/spec.py)). | Gate results keyed on what goes in, not only on the tree (§4.5.2); preflight (§4.1.3); retry with the reason, then fall back (§4.4.4); cheap no-op reviews | k-candidate fan-out per task. It buys quality with 4k rollouts; we want time |

Two of the three harnesses don't publish wall-clock numbers. The only measured speed claim found is AIDLC's review tiering comment. The targets in §2 are therefore **our** estimates (§7), and §6 measures them.

---

## 4. Design

The changes come in six levers, ordered by **hours saved per unit of risk**. Each lever is one or more slices (§8), and each can be switched off in the pack, which falls back to today's behaviour.

```
                ┌──────────────── RUNNER = SCHEDULER (deterministic) ────────────────┐
 issue/spec ──► │ triage → route profile (XS/S/M/L)   ← conductor proposes, validator  │
                │                                       disposes (harness §3.3/3.4)   │
                │ work graph: phases → steps → plan-task DAG (depends_on + files)     │
                │ plan tasks marked "Parallel group" ─► runner validates the wave      │
                │ wave session (one claude -p) ─┬─ sub-agent: Task 3                   │
                │   dispatches in one message   ├─ sub-agent: Task 4                   │
                │                               └─ sub-agent: Task 5                   │
                │ runner: attribute files → build gates once → review → commit per task│
                │ parked items: {stop, blast radius} → human inbox (non-blocking)      │
                │ gate service: parallel groups, cache by input hash, resource locks   │
                │ CI watcher: wake on completion, frees its slot                       │
                └──────────────── conformance audit on the final tree (unchanged) ────┘
```

### 4.1 Lever A: stop blocking the run on people (≈ 32 % of wall time)

**4.1.1 Make parking the default, and let the runner decide it.** `feature_lane.may_park` already parks a task and runs the tasks that don't depend on it (D12, transitively). Widen it in three ways:
- **On by default.** `hitl.park_independent` defaults to `true` in the engine defaults. Today it is `true` only in agentflow's own pack.
- **Every kind of task stop**, not only a `needs_human` the agent marked `can_continue_without`: `blocked`, `gate_failed`, `review_unresolved`, `guard_violation`, `fix_attempts_exhausted`, and a resolver that couldn't clear the stop. **The runner** decides independence from `Depends on:` plus the files the task names (`sources.task_files`): a later task that names none of the parked task's files, and doesn't depend on it, runs. The agent's flag becomes a hint, not the gate.
- **Batch members.** A stopped batch member is split out of its batch and parked on its own, instead of stopping the run. Its uncommitted work is stashed to the task's own ref, so the batch's review never sees it.

The inbox item gets a **blast radius** line: "blocks tasks 9, 14; tasks 10–13, 15–23 continue". The run reaches `verify` only when nothing is parked.

In issue-81 this turns 9.3 h of overnight idle into 9.3 h of tasks 10–23 getting built. When the human answers, only task 9 and its dependents resume.

**4.1.2 Classify every stop, and route it by class.** `resolve.md` already writes a `facts` field. The resolver now also returns a typed `stop_class`, and the runner (not the model) maps the class to an action:

| `stop_class` | Runner action | Human? |
|---|---|---|
| `environment` (a tool is missing or broken, a port is in use, Docker is down) | Run `doctor` probes and park the unit. Retry by itself when the probe goes green, and never re-ask the same question (dedupe on the probe's output hash) | Notify once |
| `flaky` (a gate passes on rerun, or a clock or order dependence) | Rerun ×2 in isolation. If it is flaky, record a `flaky:<gate>:<test>` fact and go on | No |
| `guard_false_positive` (a project guard such as `floor_guard` flags a change the resolver proves is a rename) | Park the unit, keep going, ask **one** question with the evidence diff attached | Yes, non-blocking |
| `spec_gap` (a decision is missing) | The `record_step` with the recommended answer (as today, `answer_recommended`). `boundary` kinds still go to a human | Only `boundary` |
| `impact` (a public contract breaks, a module's responsibility changes, a cross-module redesign; dsh-superagent's list) | Park the unit and its dependents, ask one question | Yes |
| `repeated_failure` (the ladder in §4.4.4 is exhausted) | Park the unit, ask one question | Yes |

**4.1.3 Preflight before any money is spent.** At `run_started`, run `doctor`'s program checks for every gate the route will use. Also run the `setup` gate set and a `--version` probe for each gate program, once, on the base tree.
- A failing probe stops the run **before** define, as one item, rather than as a `blocked_repeat` five hours in.
- The result is cached by `(tool path, version output)` for the day, so the next run's preflight takes seconds.
- In the data, this would have caught all 5 `blocked_repeat` items (6.1 h).

**4.1.4 CI is a watcher, not a human item.** `ci_failed: did not settle within 45 minutes` stops being a human item:
- the run releases its worker slot and registers a CI watcher (§4.5.5);
- the watcher wakes the run when the checks finish;
- only a red result, after `ci.fix_rounds`, reaches a human.

**4.1.5 One approval, and it doesn't block planning.** Spec approval stays the one human judgment, as AIDLC puts it: "judgment to a human". The pack already sets `phases.plan.needs_approval: false`. Three changes:
- **Plan speculatively.** While `define` waits for approval, `plan` (and its doubt review) runs on the proposed spec. The plan records the spec's blob sha.
  - If the spec is approved unchanged, `build` starts at once.
  - If the human edits the spec, the runner re-plans incrementally: the conductor gets the spec diff and the existing plan.
  - Build never starts before `approval:define`, so the obligation holds.
  - The cost of a rejected spec is one plan session ($1–5).
- **Batch approvals.** The dashboard inbox groups waiting approvals across runs into one review, like AIDLC's "grouped plan approval". A developer approves three specs in one sitting, twice a day.
- **Autonomy ramp for each approval kind:** `off → observe → auto`, as in dsh-superagent. In `observe` the runner records `would_auto_approve` with its reason (no doubt finding open, the route is S or XS, no security overlay). The scorecard compares that with what the human did. Promotion to `auto` for a lane and route class needs N agreeing samples, and any regression demotes it. This is the "autonomy ladder" of harness §3.8 (G9), made concrete. `approval:goal` and the security overlay's approval never go `auto`.

**4.1.6 A first-class human override for project guards.** When a project guard such as `floor_guard` needs a human-only approval (a `Constraints-Approved-By` trailer), the inbox item offers **"Approve the constraint"**:
- the **runner** writes the override commit with the human's name, the trailer and the evidence diff;
- it records `constraint_approved{by, key, sha}`;
- it moves its own recorded HEAD, so no `agent_commit` guard trips.

`reject_trailers` still refuses the trailer from any agent. In issue-81 this replaces 8 touches with 1.

**4.1.7 Notifications show only what a human must do.** One digest per run state change ("2 items need you; 3 runs progressing"), instead of one per stop.

### 4.2 Lever B: watchdogs on stuck work

In issue-81, `resolve-2` failed three times at ~30 min each before anyone saw it.
- **No-progress detection.** A session with no file change, no new `agent_turn` tool use and no progress for `watchdog.no_progress_min` (default 10) is ended and treated as a failed attempt. This is cheaper than the 45-min `step_timeout_min`.
- **A ceiling on resolver attempts per unit** (default 2). Then the stop is classified `repeated_failure` and parked (§4.1.2).

AIDLC's continuation hook uses the same idea: a no-progress ceiling, with the counter reset on real progress.

### 4.3 Lever C: size the route to the task (adaptive depth)

Today every feature walks the full route. The harness proposal's conductor (§3.3) and slice ES's checklist sizer already exist as **decision points**. This lever adds one decision at run start: the **route profile**.

**4.3.1 Profiles are pack data (EXECUTE/SKIP grids).** The route-shaping keys are `route.profiles.<name>.{skip, doubt_rounds, per_task_review, grill, plan_review}`. Every other knob stays as it is.

| Profile | When (runner facts + the sizer's checklist) | Define | Plan | Build | Review |
|---|---|---|---|---|---|
| **XS** | `mechanical: yes`, ≤ 2 files, no security overlay | none: the issue text is the spec | inline (`small_plan`) | one shared session | branch review only |
| **S** | ≤ 3 tasks expected, no `interface`/`risk` | draft, grilling only if Open Questions remain, **1** doubt round | 1 doubt round | inline, no per-task review | branch review |
| **M** | default | as today, **2** doubt rounds max | 1 doubt round | DAG waves (§4.4), **wave review** | branch review |
| **L** | `risk: yes`, security overlay, or ≥ 12 tasks | as today | as today | DAG waves, per-task review | branch review + the security overlay's review |

- The rule is **engine code**, like slice ES. The sizer extracts facts, the rule picks the profile, the conductor may propose **up** (never down) with a reason, and the route validator (§3.4) refuses a profile that drops an obligation.
- The judgement-step floor of §6b item 4 still applies to every profile.
- **Re-estimate mid-flight** (from AIDLC's composer). If the plan comes back with more tasks or a `risk` answer than the profile allows, the runner upgrades the profile for the remaining phases. It never downgrades.
- **The doubt rounds' stop rule.** Issue-81 ran 3 spec-review rounds and 3 plan-review rounds (~40 min). The profile caps the rounds. Findings still open after the cap ride along on the approval page, as they do today for `bounded`. The engine floor (at least 1 round for critical/high/medium findings) is kept.

**4.3.2 Review tiering.** Per-task reviews (154 sessions, 2.8 h) run on the `review` model at effort `medium`, unless the sizer flagged `risk`. AIDLC's A/B found this halves the wall time with no loss in findings. In profile M, a **review wave** replaces separate per-task review sessions: one session per wave (§4.4.4) fans out one read-only reviewer sub-agent per task, each given only its task's diff. Branch review is unchanged.

### 4.4 Lever D: parallel build inside a run (a wave session fans out to sub-agents)

`build` is 71 % of agent time, and it runs strictly one task after another. Each task is one `claude -p` session, and `task.md` tells the agent "do not … dispatch subagents". The run's `subagent_started` count is 0.

In this design, the plan marks which tasks can run in parallel. The runner checks that mark and starts **one session per wave**. That session's only job is to spawn **one sub-agent per task, all at once**, using Claude Code's `Task` tool: several `Task` calls in one message run concurrently, the way AIDLC's conductor fans out its units ([construction and swarm](https://github.com/awslabs/aidlc-workflows/blob/main/docs/harness-engineering/08-construction-and-swarm.md)). The runner keeps everything that makes the result trustworthy: which tasks go together, the gates, the review and the commits.

**4.4.1 The plan marks parallel tasks.** The plan step (`plan.md`, the `agentflow-plan` skill) gets one new optional line per task. The two lines it needs already exist:
- `**Depends on:**` (plan decision D12, `sources._depends_on`);
- `Create:` / `Modify:` / `Test:` (`sources.task_files`).
```
### Task 4: Count unread conversations by customer key
**Depends on:** Task 2
**Parallel group:** B
- Modify: `src/.../kpi.py`
- Test: `tests/integration/conversations/test_kpi.py`

### Task 5: Show the host site on staff reads
**Depends on:** Task 2
**Parallel group:** B
- Modify: `src/.../staff_reads.py`
- Test: `tests/unit/conversations/test_staff_reads.py`
```
The planner groups tasks it believes are independent. `plan_review` (the doubt review) checks the groups as part of the plan.

**4.4.2 The runner checks the mark; the mark alone is never enough.** A group becomes a **wave** only for the members that pass a deterministic check in `sources.py`:
- no member depends on another member, directly or indirectly;
- every member's `Depends on:` tasks are already committed;
- the members' file sets don't overlap, and every member names at least one file;
- no member touches a **serial path** (`parallel.serial_paths`: migrations, lockfiles, `package.json`, generated clients);
- no member was flagged `risk` by the plan sizer, and the security overlay doesn't apply to any member. Such a task always runs alone, with its own fresh review session (decision 2);
- every member has the same sized model;
- the wave has at most `parallel.max_subagents` members. Any extra members wait for the next wave.

A member that fails the check is dropped from the wave and runs alone, as today. The event is `parallel_demoted{task, reason}`. A wrong mark therefore costs speed, never correctness. A group of one is an ordinary task.

**4.4.3 The wave session.** The runner starts one session, `build/build/wave-<n>/implement`, with a new engine prompt, `task_wave.md`, and the skill `skills.map.build_wave` (default `agentflow-build`). It uses the members' shared model, at the highest effort level the plan sizer gave any member (decision 1). The prompt tells the session:
1. **Don't edit code yourself.** In **one message**, dispatch one `Task` sub-agent per task card. Each sub-agent gets:
   - its task card;
   - its **allowed files**: its declared files plus test files next to them;
   - the build brief and lessons;
   - the model the plan sizer chose for that task (the `Task` tool's `model` field).
2. **Each sub-agent's rules:**
   - edit only your allowed files, and format only those files (never `ruff format .` on the whole repo);
   - run only your own task's unit tests: no integration suite, no Docker, no database;
   - never commit;
   - finish with a short report: status, files changed, tests run.
3. **Wait for every sub-agent**, then write `result.json` with a `tasks` array: `[{n, status: done|blocked|needs_human, summary, files, tests_run}]`.

**Context is isolated; only the files are shared.** Each sub-agent starts with a fresh context window. It holds only the prompt the wave session wrote for it: its task card, allowed files, brief and lessons. It never sees the wave session's history or another sub-agent's work, and it hands back only its short report. The wave session's own context therefore stays small: task cards in, reports out.

What the sub-agents share is the run's one **worktree**, the files on disk. Their file sets don't overlap, so no worktrees, merges or cherry-picks are needed. If a coding agent can give each sub-agent its own worktree, the runner can use that instead (an option to verify per agent CLI, not assumed here).

**4.4.4 What the runner does after the session.** These are today's `lanes.py` lifecycle steps, applied per task:
1. **Attribute.** Every changed path must belong to exactly one member's allowed files.
   - A path no member declared is reverted. It is recorded as `scope_violation{task, path}`, and the member whose report names the path reruns alone.
   - The guards (`forbidden_paths`, `.agentflow/**`, HEAD moved, user-only skills) run as they do for any step.
   - Sub-agents run inside the same `claude -p` process, so the runner's `--settings` denies and hooks bind them too.
2. **Gate once per wave.** The `build` gate set runs once on the combined tree, not once per task: a further saving. On a red gate, the fix loop resumes the wave session with the gate output, and the session dispatches a fix sub-agent for the member the failure points to. The member's first fix attempt is escalated one level (slice ES).
3. **Review.** One **review wave** session fans out one read-only reviewer sub-agent per task. Each reviewer gets only its task's diff (`git diff <wave start> -- <its files>`). Blocking findings reopen only that task, through today's single-task fix-and-re-review path. Profile L (§4.3) keeps a fresh, separate review session per task.
4. **Commit per task, in plan order.** The runner stages only that task's files (`git add -- <files>`) and makes one `Task N: <title>` commit with its ticked box. `task_committed` stays one commit per task, as today.
5. **Park a member that stopped.** A member that reports `blocked` or `needs_human`, or whose fix attempts run out, has its files saved to its own ref and reset to the wave start. It is parked (§4.1.1), and the other members commit. When it is answered, it reruns alone.

**4.4.5 Seeing and metering the sub-agents.** `agents.py` already turns each `Task` call into `subagent_started`/`subagent_finished` events with the `tool_use_id`, model and description (`agents.py:279–324`), and `state.py` keeps them on the step's row. The wave adds:
- the task number to the sub-agent's description (`Task 4: …`), so each event maps to a task;
- a per-task cost estimate from the usage lines that carry that `tool_use_id` as their parent;
- a check that each sub-agent's `model` matches the sized model. A mismatch is a `guard_tripped` and the wave fails.

The wave's budget is the sum of its members' `task_budget_usd`.

**4.4.6 Failure and recovery.**
- **The wave session fails, times out or rolls over.** The runner attributes what finished: a member with a report and a clean attribution goes on to gates. Every unfinished member reruns **alone**, as today. A wave is never resumed half-way. Its timeout is the largest member timeout plus a margin.
- **The ladder per task** (from AIDLC's build-and-test):
  1. ≤ `fix_attempts` fix sub-agents;
  2. one targeted rerun of that task alone, with the failing gate output and the reason the last attempt was rejected ("retry with reason", as in harness2);
  3. park it as `repeated_failure`.

  The rest of the wave and the run go on.
- **Codex.** `codex exec` has no `Task` tool, so with `agents.codex` every task runs alone. Lever F's slots and the runner-level worktrees below are Codex's route to parallelism.

**4.4.7 What changes in the engine and the packs.**
- `task.md` keeps "do not dispatch subagents" for single-task sessions. The wave has its own prompt.
- Vendored `subagent-driven-development` says "Never dispatch multiple implementation subagents in parallel (conflicts)". That rule is right for a session that chose its own tasks. A wave's tasks were checked for disjoint files by the runner, so the wave maps to its own skill (`skills.map.build_wave`) and never to a pack's SDD skill.
- `sources.py` parses `**Parallel group:**`. `plan.md` and `plan_review.md` learn the line. `explain` shows a wave as `wave-<n>: tasks 4, 5`.

**4.4.8 Expected effect.** Plans in the data have 9–24 tasks. With waves of 3–4 and a typical independent width of 2–4, implement wall time drops about 2–2.5×. Serial-path tasks and the wave's slowest member remain. Build gates run once per wave instead of once per task, and reviews run as a wave.

**4.4.9 Later, if shared-tree conflicts show up: runner-level worktrees.** If the scorecard shows `scope_violation` or cross-member test interference in more than a few percent of waves, step up to runner-started parallel sessions. Each member gets its own sibling worktree and its own `claude -p` session, and a merge train cherry-picks each task onto the run branch in plan order. This is also the route for Codex. It costs more engine work (worktrees, merges, a session per member), which is why it is the second step, not the first.

### 4.5 Lever E: gate economics (17 % of wall time, plus the repeats in CI)

**4.5.1 Parallel gate groups.** The gates of a set run one after another today. New optional keys:
- `gates.<name>.group`: gates in different groups run at the same time; gates in the same group run in order.
- `gates.<name>.resource`: a named lock, e.g. `postgres`. Gates that share a resource never overlap, in a run or across runs (§4.6).

In this pack, `webadmin` (biome, types, vitest, two builds) and `backend` (pyright, unit, integration, migrations, eval) are independent. The verify set's wall time becomes max(group) instead of the sum, about −35 % on the measured runs.

**4.5.2 Gate results keyed on their inputs, not only on the tree.**
- `gates_reused` already skips a gate whose tree hasn't changed. Extend the key to `hash(cmd, env subset, files matched by gates.<name>.inputs)`. A fix that touches only `webadmin/**` then reuses every backend gate result, and the other way round.
- The conformance audit accepts a reused result only when the input hash equals the hash on the **final tree**. No evidence is weakened: the gate *would* produce the same result, by construction of its declared inputs.
- `validate` warns about a gate with no `inputs`, which keeps today's behaviour.

**4.5.3 Three gate tiers, with the full suite once.**

| Tier | When | Gates (this pack) |
|---|---|---|
| fast | each task's referee, each merge | ruff, format, pyright and tsc **of the changed package**, the task's own tests |
| affected | each wave, and each fix of a finding | `gates.<name>.affected`: a command template that takes the changed paths, e.g. `pytest {changed_tests}` |
| full | **once**, on the final tree before `finish`, and again only if the tree changed after it | the `verify` set |

`verify/fix_findings`, `review/fix_findings`, `ship/finish` and `ship/ci/fix-N` drop from `full` to `affected`. The conformance audit still requires a passing **full** `gates:verify` on the final tree, and runs it itself if the tree moved. That keeps the floor and removes the 2–4 extra full runs a feature pays today.

**4.5.4 CI as the second verifier, not a third full run.** `finish` opens the draft PR **as soon as `verify` is green**, so CI runs while `branch_review` runs. Today CI starts only after review. CI never stands in for the local `verify` run (decision 3). The overlap alone removes most of the CI wait.

**4.5.5 CI waits wake the run instead of polling it.** `ci_lane` registers a watcher, through `gh run watch` or a poll in the queue daemon, and gives its worker slot back. That fits dsh-superagent's "move long work to the background, wake on completion". A run waiting on CI costs no slot and no attention.

### 4.6 Lever F: several features at once, safely

`agentflow queue --max-parallel` (default 2) exists, and runs did overlap 3 times in the data. But the pack pins shared resources: `POSTGRES_HOST_PORT: "55432"` and compose project `tivacare-test`. Concurrent runs, and concurrent tasks under §4.4, would share one database.

- **Resource slots.** `slots.count: N` gives each run, and each worker within a run, a slot number. The pack's `defaults.env` and gate commands may use `{slot}`:
  ```yaml
  slots: {count: 4}
  defaults:
    env:
      POSTGRES_HOST_PORT: "5543{slot}"
  gates:
    postgres_up: {run: "docker compose -p tivacare-test-{slot} up -d postgres --wait"}
  ```
  A resource that can't be multiplied is a `gates.<name>.resource` lock instead (§4.5.1).
- **Budgeting attention, not just slots.** Of the queue's slots, `queue.human_slot: 1` stays free for a run started by hand (dsh-superagent's reserved slot). The dashboard's "In progress" column shows each run's parked items, so one person can supervise 3 features from one inbox.
- **Dependency-aware feeding.** `module_queue`'s `blocked_by`/`parallel_with` (the issue-dependencies proposal) already decides which features may run side by side. This lever only lifts the resource collision that makes `--max-parallel > 1` unsafe today.

---

## 5. What stays exactly as it is (quality guarantees)

- **The conformance audit** (`conformance.py`) on the final tree before the push. Every obligation must show passing evidence whose tree or input hash equals the final tree's. Parallel build, gate reuse and CI evidence are all checked by this one audit, so no lever can ship an unverified tree.
- **The runner is the only committer.** Wave sessions and their sub-agents never commit; the runner commits each task's own files, one commit per task.
- **The guards**, run on every wave session as on any step, plus the new `scope_violation` for files outside a task's declaration.
- **Engine floor**:
  - `gates.verify` stays non-empty;
  - at least one refine round on critical, high or medium findings;
  - the judgement-step effort floor;
  - `.agentflow/**` stays forbidden;
  - user-only skills stay refused;
  - `approval:goal` and the security approval can never be `auto`.
- **The golden `explain` files.** With `parallel.max_subagents: 1`, `route.profiles` unset and no gate groups, `explain` output is byte-identical. Every lever is opt-in for a pack until its slice proves itself on the scorecard, then defaults on in the engine.

---

## 6. Measuring it

New events:
- `unit_parked{key, stop_class, blast_radius}` and `unit_unparked`;
- `route_profile{profile, facts, proposed_by, upgraded_from}`;
- `wave_started{wave, tasks}`, `parallel_demoted{task, reason}` and `scope_violation{task, path}`;
- `gate_cache_hit{gate, input_hash}`;
- `slot_acquired{slot, resource}`;
- `watchdog_fired{key, idle_min}`;
- `would_auto_approve{kind, reasons}`.

New `signals.py` keys and scorecard KPIs:
- **`lead_time_h`** (`run_started` → `pr_marked_ready`);
- **`human_blocked_share`** (time with no running agent, no gate and an open item, divided by wall time);
- **`human_touches`**;
- **`agent_parallelism`** (agent-seconds per wall-second during build);
- **`full_suite_runs`**;
- **`gate_cache_hit_rate`**;
- **`park_resume_rework`** (tasks redone after unparking).

`agentflow metrics` gets the wall-time breakdown of §1.1 per run, so the evidence in this proposal comes from a command, not a one-off script.

Each slice is measured against the scorecard baseline frozen before slice T1, on at least 5 feature runs. A slice that regresses the conformance SLO or `park_resume_rework` is reverted before the next one starts.

---

## 7. Expected effect (estimate, for a 15–23-task feature that takes ~9 h today)

| Segment | Today | After | Lever |
|---|---|---|---|
| Define + plan | ~1.5 h (3 + 3 doubt rounds) | ~0.6 h; planning overlaps the spec approval | C, A.5 |
| Waiting for spec approval | 0.1–1 h, blocking | overlapped with planning | A.5 |
| Build (implement + per-task review) | ~2.5–3.5 h, serial | ~1.0–1.3 h (3 workers, review pipelined) | D, C.2 |
| Stops during build | 0–10 h; the run is fully blocked | the parked unit only; the rest continues | A, B |
| Verify + review + fix findings | ~1–2 h (full suite 2–4×) | ~0.5 h (full once, in groups, affected for fixes) | E |
| Ship + CI | 0.2–7 h, often polling or a human item | ~0.3 h, CI overlapping review | E.4, E.5 |
| **Lead time** | **~8–9 h** | **~2.5–3 h** | |

Throughput multiplies on top: 3 features in flight (Lever F) with one shared inbox gives **~3 PR-ready features per half day**, and the developer acts **once per feature**.

Cost: parallelism adds no tokens. The things that do:
- speculative planning: a plan session thrown away when a spec is rejected;
- merge-train retries.

Fewer review rounds, reviews at medium effort and fewer full-suite fix sessions save tokens. The net is expected to be within ±20 %; `budget_*` events track it.

---

## 8. Rollout

Slices are ordered by **hours saved per unit of risk**. Each slice is one PR on the feature lane of agentflow itself, with a plan in `docs/plans/`.

| Slice | Content | Risk | Expected saving | Status |
|---|---|---|---|---|
| **T0 Measure** | Wall-time breakdown in `metrics`; `lead_time_h`, `human_blocked_share` and `full_suite_runs` in signals and the scorecard; freeze the baseline | none | makes the rest measurable | shipped 2026-11-15 (PR #29) |
| **T1 Never block on the environment** | Preflight (A.3); `stop_class` from the resolver plus routing (A.2) for `environment` and `flaky`; dedupe of `blocked_repeat`; watchdog (B); CI watcher (A.4, E.5) | low | ~20 % of human-wait hours | shipped 2026-11-15 (PR #30) |
| **T2 Gate economics** | `group`, `resource`, `inputs` and `affected` for gates; tiers; one full suite on the final tree, enforced by the conformance audit (E.1–E.3); early draft PR (E.4) | medium (the conformance audit gets new evidence kinds) | −50–60 % gate wall time | shipped 2026-11-16 (PR #32) |
| **T3 Route profiles** | Profiles + rule + mid-flight upgrade (C.1); review tiering (C.2); capped doubt rounds | medium (quality); guarded by the floor and `observe` first | −30–60 % define/plan; small features ≤ 45 min | shipped 2026-11-18 (PR #39); `route.mode` defaults to `off`. Deferred: M's wave review (needs T5), XS without a spec draft, the profile on the dashboard |
| **T4 Park and continue** | `park_independent` on by default and widened to every task stop, with the runner deciding independence; batch members park alone; a blast radius on each item (A.1); the human override for guards (A.6); batched approvals and a digest (A.5, A.7) | medium | most of the remaining human-wait hours | not started |
| **T5 Parallel build** | Human pre-flight with a real `claude -p` (decision 1); `**Parallel group:**` in the plan; the runner's check of the mark; wave sessions that fan out sub-agents; attribution, build gates once per wave, a review wave, commits per task (D) | high (concurrency); `max_subagents: 1` until the scorecard is green on 5 runs | build 2–2.5× faster | not started |
| **T6 Slots and multi-run** | `slots` and `{slot}`; the human slot; resource locks across runs (F) | medium | 3 features in flight | not started |
| **T7 Speculative planning and the approval ramp** | Plan while the spec awaits approval; `observe → auto` per approval kind, from the scorecard (A.5) | medium | overlaps the last human wait | shipped 2026-11-22; autonomy.speculate and autonomy.approvals default off |

T1 and T2 change nothing about how agents work, and they recover the most hours for the least risk. They go first. T5 depends on T4, because parking is how a failed member gets out of a wave. It doesn't need T6: sub-agents don't run integration tests or Docker, and the runner's gates still run one at a time per run.

Every new config key ships with its four companions (CLAUDE.md invariants):
- a Settings field, or a `_PROTECTED_KEYS` entry;
- `validate` checks;
- a `test_config_edit` case;
- a playbook §14 line.

The keys are:

| Key | Default | Protected? |
|---|---|---|
| `parallel.max_subagents` | 1 (off) until T5 is green on 5 runs, then 3 | Form field; `validate` refuses more than 6 |
| `skills.map.build_wave` | `agentflow-build` | Form field |
| `parallel.serial_paths` | [] | Form field |
| `route.profiles` | engine default grid | protected; a pack may narrow it, never widen it below the floor |
| `gates.<n>.group` / `.resource` / `.inputs` / `.affected` | unset | Form fields |
| `slots.count` | 1 | Form field |
| `queue.human_slot` | 1 | Form field |
| `watchdog.no_progress_min` | 10 | Form field |
| `autonomy.approvals.<kind>` | `off` | protected; promotion needs a green scorecard |

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| Parallel tasks conflict semantically (no shared file, but one changes a contract the other uses) | Plan review checks `Depends on:` and the groups; the build gates run on the **combined** tree; the full suite runs before finish. A failure points at a member, which reruns alone |
| Sub-agents in one tree step on each other (a formatter run on the whole repo, a shared build cache, two test runs at once) | Allowed files only; format only your files; no integration tests, Docker or database in sub-agents; the runner's attribution reverts stray paths. Persistent trouble moves the pack to runner-level worktrees (4.4.9) |
| The wave session ignores the runner's choice (dispatches other tasks, another model, or edits code itself) | Every `Task` call is an event; a model or task mismatch is a `guard_tripped`; attribution catches edits outside the members' files; the wave then fails and its members run alone |
| Flaky tests under parallel gates, or shared state | `resource` locks; `{slot}` isolation; `flaky` stops are recorded and rerun in isolation, never silently passed |
| Route profiles under-size a risky change | The rule is engine code; the conductor may only go **up**; mid-flight upgrade; the security overlay forces L; the `undersized` signal from slice ES covers profiles too |
| Speculative plans waste money | Only after `spec_review` is clean; capped at one plan session per spec revision; off by default until T7 |
| Gate reuse by input hash hides a dependency the pack didn't declare | Reuse only for gates with explicit `inputs`; the final full run is never skipped when the tree changed after it; `validate` warns |
| Parked work piles up unseen | A blast-radius line on every item; the digest; `park_resume_rework` on the scorecard; a run can't reach `verify` with parked units |
| More moving parts in the runner | Each lever is behind a key whose default is today's behaviour; golden `explain` files are pinned; `fake_agent` gets a concurrency mode for tests |

## 10. Decided behaviour

(The author's recommendations, accepted 2026-11-15 to settle this proposal's six open questions.)

1. **Effort per sub-agent isn't needed for T5.**
   - A wave takes only members with the same sized model. It runs at the highest effort level among them. Over-sizing is the safe direction, as in §6b's fallbacks: it costs tokens, never rework.
   - Risk-flagged tasks never join a wave (decision 2), so the gap within a wave is at most `low` against `medium`.
   - T5's first task is a human pre-flight with a real `claude -p`, like slice ES's flag checks. It confirms two things:
     - that several `Task` calls in one message run at the same time;
     - whether a sub-agent's effort can be set on its own.
   - If concurrency doesn't hold, T5 stops and the pack moves to runner-level worktrees (4.4.9). If per-sub-agent effort exists, the wave passes each member its own level, and the same-model rule stays.
   - Rejected: waiting for per-sub-agent effort before building T5.

2. **Risky work never runs in a wave.** A task the plan sizer flagged `risk: yes` (concurrency, persisted state, security, a data migration, money), or one the security overlay applies to:
   - always runs alone;
   - gets its own fresh review session, never a sub-agent in a review wave.

   This holds in every route profile.
   - Rejected: letting a risk task join a wave but giving it its own review. A shared-tree implementation is exactly where risky changes need the least interference.

3. **There is no `ci_equivalent` evidence.**
   - CI never satisfies `gates:verify`. The local full suite runs once, on the final tree, as the conformance audit requires.
   - The time saving comes from overlap: the draft PR opens as soon as `verify` is green, so CI runs while `branch_review` runs (E.4), and CI wakes the run when it finishes (E.5).
   - The key is dropped from the config table.
   - Rejected: CI as optional evidence. It would add an evidence kind to the conformance audit and a protected key. CI's environment differs from the local one. And the overlap already removes most of the wait.

4. **`parallel.max_subagents` defaults to 1 (off) until T5 is proven, then 3.**
   - The engine default becomes 3 once T5's scorecard is green on 5 real runs. 3 is the low end of the data's typical independent width (2–4) and matches dsh-superagent's default.
   - A pack may raise it to 6. `validate` refuses more, because the wave waits for its slowest member and shared-tree contention grows with each member.
   - Rejected: 4 as the default. It gains little over 3 at the widths in the data, and it adds contention.

5. **A project guard's false positive never passes by itself.**
   - When a resolver proves a `floor_guard` finding is only a rename, the task is parked. The run goes on, and the inbox item carries the evidence diff and the one-click **"Approve the constraint"** (A.6).
   - The guard stays a human-only constraint (`Constraints-Approved-By`). The improvement is that it no longer stops the run, and approving it takes one touch instead of eight.
   - Rejected: auto-pass on a proven rename. The resolver is a model, and the guard exists precisely so that a model can't remove an assertion.

6. **The runner never makes groups itself; it only points them out.**
   - The plan's `**Parallel group:**` mark is the planner's judgement on meaning; the runner's check covers only files and dependencies.
   - What the runner does: it computes `parallel_candidates`, the ungrouped tasks with no dependency between them and no shared files. It hands them to `plan_review` as a hint ("tasks 6 and 7 could share a group"). The doubt review may then raise a minor finding, and the plan's reconcile step may add the group.
   - The scorecard counts the candidates, so a later slice can revisit this with data.
   - Rejected: grouping by itself. Two tasks with no shared file can still change the same contract.

## Open Questions

None. The six open questions were settled on 2026-11-15 (see §10 Decided behaviour).

## Verification

- **T0:** `agentflow metrics --all` prints the §1.1 breakdown, and its totals match this proposal's figures for the 16 runs to within 5 %.
- **T1:**
  - A fixture pack whose gate program is missing stops at preflight with one item, before `define`.
  - A fake agent returning `stop_class: environment` twice produces one inbox item, not two.
  - A session that writes nothing for `no_progress_min` gets `watchdog_fired`.
- **T2:**
  - In a fixture, a fix touching only `frontend/**` reuses the backend gates (`gate_cache_hit`).
  - The audit refuses a reused result whose input hash differs from the final tree's.
  - `full_suite_runs == 1` on a clean run.
- **T3:**
  - `explain` with no `route.profiles` is byte-identical to the golden files.
  - An XS fixture runs no grilling and no doubt round.
  - A plan that grows past S's task cap upgrades to M (`route_profile` with `upgraded_from`).
- **T4:**
  - In a 5-task fixture where task 2 stops with `blocked` (not only `needs_human`), tasks 1, 3, 4 and 5 (none depending on 2) finish.
  - The guard override commits once, with no `agent_commit` trip.
  - The item lists the blast radius.
  - Answering it resumes task 2 only.
  - `verify` doesn't start until nothing is parked.
- **T5:**
  - On a fixture plan with tasks 2 and 3 in `Parallel group: A`, disjoint files and `max_subagents: 2`, one wave session runs. Its scripted `fake_agent` emits two `Task` calls in one message, and two `subagent_started` events are recorded before either `subagent_finished`.
  - The commits are `Task 2` then `Task 3`, each holding only its own files.
  - A grouped task that shares a file with another member, or depends on it, gets `parallel_demoted` and runs alone.
  - A member writing outside its allowed files gets `scope_violation`; the path is reverted and the member reruns alone.
  - One member reporting `blocked` is parked, and the other member commits.
  - With `agents.codex`, or `max_subagents: 1`, `explain` output is unchanged.
  - A grouped task the plan sizer flagged `risk` gets `parallel_demoted{reason: risk}`, runs alone and gets its own review session.
  - Two ungrouped tasks with disjoint files and no dependency appear as `parallel_candidates` in `plan_review`'s input.
  - `validate` refuses `parallel.max_subagents: 7`.
- **T6:** Two concurrent fixture runs with `slots.count: 2` get different `{slot}` values, and a `resource`-locked gate never overlaps across them.
- **All slices:**
  - The full suite is green.
  - `validate` reports 0 warnings on this repository's pack and the fixture packs.
  - The scorecard shows no conformance regression across 5 real feature runs before the next slice starts.
