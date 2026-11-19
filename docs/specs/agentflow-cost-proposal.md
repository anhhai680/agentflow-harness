# Proposal: agentflow cost — fewer tokens per feature, and features that finish inside the usage window

Status: approved (2026-11-18)

Related: `docs/agentflow-harness-proposal.md` (§3.11 cost governance), `docs/specs/agentflow-throughput-proposal.md` (§4.2 watchdogs, §4.3 route profiles, slice T3), `docs/specs/agentflow-doubt-review-proposal.md` (doubt rounds), playbook §14 (pack reference).

## Context

A feature run spends too much, and the cost shows up as a hard stop: the subscription's usage window runs out before the feature reaches a PR. On 2026-11-17, five feature runs of the `customers-support-agentic-ai` pack ran side by side. They hit the five-hour window twice (10:22 and 14:46 UTC), and every run then held for 2.5–3 hours, most of them in the middle of `plan`.

The developer suspected three causes: too many turns, too little prompt caching, and sessions that keep going with no limit. The data below confirms two of them and corrects the third. Caching works within a session (94% of input tokens are cache reads). The money goes to **cache writes**, which are 58% of spend:
- every write uses the 1-hour TTL, at 2× the input price;
- every fresh session rewrites a ~25K-token prefix that no other session can reuse;
- tool output grows the context, and every new token is written once and then read on every later turn.

This proposal keeps the core rule ("the model proposes, the harness disposes") and every engine-floor guarantee. It changes **how** the runner starts sessions (flags, environment, prompt layout, tool sets and caps) and **when** it starts them (the usage-window governor). It doesn't change **which** steps run. Choosing fewer or cheaper steps is already the job of the route profiles and model sizing (T3).

---

## 1. Evidence

### 1.1 Method and data set

- **Sources:**
  - the 6 runs still in the state dir (`~/.local/state/agentflow/customers-support-agentic-ai-*/runs/`): 5 features and 1 retro, 2026-11-17 → 2026-11-18;
  - their runner transcripts: 434 invocations;
  - their `events.jsonl`;
  - the Claude Code session files of the run worktrees (`~/.claude/projects/*-worktrees-*/*.jsonl`): 317 sessions and 5,567 API messages, with full prompts and tool results.

  The 16 runs of 2026-11-07 → 11-15 that the throughput proposal measured are no longer on disk.
- **Prices:** list prices from the engine's own table (`defaults/config.yaml` `agents.claude.prices`). The account is a subscription (`apiKeySource: none`), so the dollars are a *list-price equivalent*: a stand-in for how much of the usage window a run consumes. Anthropic doesn't publish how cache reads and writes are weighted against subscription limits. §6 treats the dollar ratios as the best available proxy.
- **Counting:** per invocation, from `model_usage_since_last_result`. Claude's `total_cost_usd` is cumulative over every `--resume` of a session, so summing it counts resumed sessions twice (459 vs 229). The engine already handles this correctly (`state.cost_added`). Only a naive sum is wrong.
- **Probes:** I ran 30 `claude -p` calls on Haiku with `--max-turns 1` (CLI 2.1.327, under one cent in total) to measure the session prefix under different flags (§1.6).

### 1.2 Where the money goes

**5 features: $235 list-price equivalent.** Per message, by token class:

| Bucket | Share | Note |
|---|---:|---|
| Cache **writes** | **58 %** | 23.3M tokens, **all 1-hour TTL** (2.0× input price); 5-minute writes: 0.06M |
| Cache reads | 22 % | 335M tokens at 0.1× input |
| Output (incl. thinking) | 20 % | 3.2M tokens |
| Uncached input | ~0 % | |

By model: Opus 64% ($151, the define and plan phases), Sonnet 36% ($85). T3 has since made Sonnet the default for planning. At the same token counts, that alone brings these 5 features to **≈ $160, or $32 per feature**. That is the baseline the estimates in §6 start from.

By step kind (per invocation; 428 invocations with a result):

| Step | Sessions | Share | Turns / session (p50 · p90 · max) |
|---|---:|---:|---|
| `build/task/implement` | 101 | 21 % | 16 · 37 · 84 |
| `define/spec_review` (doubt) | 26 | 16 % | 26 · 34 · 49 |
| `plan/plan` | 20 | 15 % | 22 · 43 · 89 |
| `define/draft` | 30 | 12 % | 8 · 32 · 40 |
| `plan/plan_review` (doubt) | 22 | 12 % | 21 · 31 · 40 |
| `build/task/review` | 79 | 9 % | 7 · 12 · 25 |
| everything else (grill, apply, reconcile, sizers, verify, branch review, ship) | 150 | 15 % | |

**Review-type sessions (spec_review, plan_review, task review, branch review) are 38% of spend.** By phase: define 34%, build 31%, plan 27%, ship 4%, review 3%, verify 1%. `verify` isn't a cost center.

### 1.3 Why writes dominate

1. **Every write uses the 1-hour TTL.** Claude Code picks the 1-hour cache for this account. It costs 2.0× the input price, against 1.25× for the 5-minute cache. Turns within a session follow each other closely: only **43 of 5,547** main-thread calls came more than 5 minutes after the previous one (18 after more than 15 minutes, 4 after more than an hour). Replaying the history with a 5-minute TTL, and pricing every call after a gap of more than 5 minutes as a full rewrite of its prefix, gives **−17.4%** total cost.

   Gaps *between* sessions are short too:
   - within a run, 2% of the gaps between one session's last call and the next session's first call are over 5 minutes (median 5 s);
   - across the machine, 2% (median 0 s), because parallel runs keep the shared prefix warm.
2. **Each fresh session writes a ~25K-token prefix that no other session reuses.** On its first call, a fresh session reads a median of 13K tokens from cache (the static system prompt and tools) and **writes a median of 25K** (307 fresh sessions, 7.0M tokens: 30% of all writes). Claude Code puts the repository context (CLAUDE.md, the skills listing, MCP instructions, the auto-memory and the git status) into the **first user message**, together with the step prompt. That makes the whole block one cache segment, so a different prompt rewrites all of it (probe K in §1.6). The runner's step prompt itself is only ~5K tokens (median 21K chars).
3. **Tool output grows the context, and the context is re-read every turn.** In the session files:
   - `Read` is 61% of the tool output in implement sessions, and 74% in plan reviews.
   - A file is read again within the same session in 28% of reads in `implement`, 34% in `plan` and 38% in `draft`.
   - Across sessions of the same run, `handoff.md` was read 71 times, `progress.md` 58, and each run's spec ~23. This is shared context that every session pays for again.
   - Shell searches (`grep`/`find`/`ls` through Bash) put 1.2M chars into implement sessions, 0.9M into spec reviews and 0.7M into plans.
   - Implementers ran `pytest` 262 times, lint and type checks 331 times and `vitest` 60 times inside their sessions (0.6M chars), and the runner's gates run them again anyway.
   - **Reviewers ran `pytest` 35 times** and used `Write` 80 times, although a review's gates have already run.
   - 128 tool results were over 20K chars.
4. **Sessions compact.** There were 102 auto-compactions, all at ~110K tokens (`autocompact_pct: 60` of the 200K cap): `plan` 26, `implement` 25, `spec_review` 18, `draft` 15 and `plan_review` 13. Each one is an extra summarisation call over the whole context, followed by re-reads.
5. **Doubt rounds repeat full reviews.** There were 26 spec reviews and 22 plan reviews for 5 runs, and 18 `phase_rejected` events in define and plan. Every round is a fresh session that explores the codebase again (spec_review p50: 26 turns).

### 1.4 The usage window

- Both window hits stopped runs in the middle of a session: `plan/plan` twice, exit 1, at $5.7–5.9 per session so far, and once in `ship/ci/fix-1`. The runner held each one (`run_held`, `reason: usage limit`) and resumed it after the reset.
- A resume after a 2.5-hour hold finds the cache expired, so it rewrites the whole context it had built up.
- The stream already reports the window on every call (`rate_limit_event`, read into `acc.limit`): `unifiedWindows.five_hour.utilization` and `seven_day.utilization`, with `resetsAt`, and `allowed_warning` near the edge. There were 917 `allowed`, 50 `allowed_warning` and 7 `rejected` events.
- `seven_day` was already at 0.41 early in the day, so the weekly window is the next ceiling.
- Agentflow spent $50–100 of list-price equivalent per five-hour window before it was refused. The developer's other Claude Code sessions draw on the same window.
- **Nothing schedules against this signal.** The runner starts new runs, phases and sizers at 95% utilization just as it does at 5%. Five runs in parallel all reach the limit together, and all of them stop half-done.

### 1.5 What is already in place

- **Resume within a step:** a fix attempt, a stop and a refine round each resume the step's own session (`lanes._Turn.resume`), and the probe shows that resumes read almost everything from cache: median 64K read and 3K written on the first call.
- **Batching:** a small plan shares one session (`small_plan.shared_session`), and plan batches (`batch: {max_tasks: 3, max_files: 3}`) share one session. Batching applied to 2 of the 5 runs.
- **`max_turns`:** each agent has one (`agents.<name>.max_turns`, unset by default). Sizers get their own cap and read-only tools (`AgentCall.read_only`: `--tools Read,Grep,Glob --strict-mcp-config`).
- **Watchdog:** on at 15 minutes since T3 (`watchdog.no_progress_min`), with a longer limit while a tool call is open.
- **Rollover and compaction policy:** `context.*`.
- **T3:** Sonnet planning defaults and route profiles `XS`–`L`, which skip steps and cap doubt rounds. They are off by default (`route.mode`).

### 1.6 What the CLI can do (probes, CLI 2.1.327)

Every row is a single `claude -p` call. Each pair ran back to back, with a short prompt.

| # | Flags / env | Cache write | Cache read | Reading |
|---|---|---:|---:|---|
| A4/A5 | defaults, same prompt twice | 0 | 33.8K | an identical session is fully cached |
| K/K2 | lean (below), **different** prompt | 11.4K | 19.0K | the first user message (context + prompt) is rewritten in full |
| L1/L2 | `--exclude-dynamic-system-prompt-sections --strict-mcp-config`, new prompt | 18.2K | 13.3K | skills listing and CLAUDE.md are still rewritten |
| M1 | the same + `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` | 10.1K | 13.3K | CLAUDE.md is ~8K tokens per session in this repo |
| N2/N3 | the same + CLAUDE.md and the engine preamble in `--append-system-prompt-file`, new prompt | 10.1K | **22.7K** | the appended system prompt **is reused across sessions** |
| C2 | lean + `--disable-slash-commands`, other cwd | 3.0K | 19.0K | the skills listing is ~7K; the rest is ~3K |
| D2 | lean + `--tools Read,Grep,Glob` | 1.5K | 4.8K | 20 tool schemas are ~15K tokens of prefix |
| P1/P2 | defaults vs `--setting-sources project,local` | 19.5K → 6.0K | 14.3K → 23.2K | drops the user's plugins (2), MCP servers, hooks (16 events) and their skills (51 → 41 skills, 37 → 24 tools); the user file has no permissions or env |
| B1 | `--max-budget-usd 0.0015` on a 4-call task | — | — | ends after the turn that crosses the cap ($0.0027): `subtype: error_max_budget_usd`, `terminal_reason: budget_exhausted`, exit 1, **no result text** |
| B2 | `--resume` with a $0.002 cap on a session that had already spent $0.0034 | — | — | succeeds: the cap counts **per invocation**, not the session's total |
| — | `FORCE_PROMPT_CACHING_5M=1`, or `CLAUDE_CODE_PROMPT_CACHE_TTL=5m` | — | — | writes become `ephemeral_5m` |
| — | `--permission-mode dontAsk --tools Read,Grep,Glob,Bash --allowedTools "…,Bash(git status *),Bash(git diff *),…"` | — | — | `git status` and `ls` run; `touch` is denied |

"Lean" here means `--exclude-dynamic-system-prompt-sections --strict-mcp-config --disable-slash-commands`.

What these show:
- A session's unique prefix can drop from **~22K to ~3K tokens** plus the step prompt.
- Stable text placed in the system prompt is a cross-session cache **read** (0.1×) instead of a write (2.0×).

Two caveats:
- `FORCE_PROMPT_CACHING_5M`, `CLAUDE_CODE_PROMPT_CACHE_TTL`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY` and `BASH_MAX_OUTPUT_LENGTH` are undocumented environment variables, so §3 never relies on them blindly. The runner checks their effect in the stream it already parses.
- `--bare` can't be used, because it refuses `bypassPermissions`.

---

## 2. Goals and non-goals

**Goals** (measured with the signals of §3.1, against the frozen scorecard baseline):

1. **−30% or more list-price-equivalent spend per feature run**, with the same lanes, the same gates and the same obligations.
2. **Cache-write share of spend ≤ 35%** (from 58%), and a **median fresh-session prefix write ≤ 10K tokens** (from 25K).
3. **No feature left half-done by the usage window.** No run starts a new phase or task with five-hour utilization above the soft mark, and runs that are already going finish first.
4. **Bounded sessions.** Every agent session has a turn cap. A session that hits it ends with a result or a named stop (`turn_cap`), never by burning on.
5. **Quality doesn't drop.** Scorecard conformance stays at 100%, and `gate_first_pass:*` and the reviews' blocking-finding rate don't get worse.

**Non-goals:**
- Choosing models or effort. T3 and the sizer own that, and the core rule forbids priors from earlier runs choosing a model (harness proposal §6b).
- Skipping steps. Route profiles own that.
- Changing what a reviewer is allowed to conclude, or merging the doubt review into the author's session.
- Codex: every lever here is a no-op for `agents.codex` unless it says otherwise. Codex already has a read-only sandbox for read-only calls.

---

## 3. Design

Seven levers, each one testable on its own. L1 comes first: it's what makes all the others measurable.

### 3.1 L1 — Measure the cache, the turns and the window (no behaviour change)

Today `agent_exited` carries only `cost`, and `agent_turn` carries only total `tokens`. Proposed additions:

- **`agent_exited.usage`:** this invocation's split, summed over every result's `model_usage_since_last_result` in the invocation, else estimated from the messages' own usage (`estimated: true`); never from `modelUsage`, which is the session's cumulative total: `input`, `cache_read`, `cache_write_5m`, `cache_write_1h`, `output`, `turns`, `terminal_reason`, plus `prefix_write`, the first call's `cache_creation_input_tokens`.
  - *Amended for the C0+C1 plan:* the split is summed over the invocation's results rather than read with a fallback to `modelUsage` (a resumed session's `modelUsage` counts its earlier invocations too), and the turn count's key is `turns`, not `num_turns`.
  - This lands in `agents.py` (`StreamAccumulator`, `parse_claude_line`) and `state.py`, and is read in `metrics.py`.
  - The engine already prices sessions from usage when no cost is reported, so no new parsing class is needed.
- **`usage_window`:** a new event, written when the stream's `five_hour` or `seven_day` utilization moves by 5 points or more, or its status changes. It carries `{window, utilization, resets_at, status}`. The run's `state.json` keeps the latest values (`state.usage_window`).
- **Signals** (`signals.py`, fixed vocabulary), each usable as a lesson `target` and shown in `agentflow scorecard` and on the Metrics tab:
  - `cache_write_share`
  - `prefix_write_tokens` (the median over fresh sessions)
  - `session_turns_p90:<step-kind>`
  - `tool_output_kchars:<step-kind>`
  - `compactions`
  - `usage_holds`

  *Amended for the C0+C1 plan:* the names follow the vocabulary's `family[:param]` grammar, and `tool_output_kchars:<step-kind>` is per session that reached the model, so a session with no turns doesn't dilute it.
- **Metrics tab:** a "Where the tokens go" panel, per phase and per step kind: write, read and output; turns; the prefix write. It's the §1.2 tables, built from the run's own events.

**Acceptance:** a fake-agent run whose script emits known usage shows the exact split in `metrics` and the signals. `explain` and its golden files don't change.

### 3.2 L2 — Cache economics: TTL and a stable, shared prefix

**L2a. A 5-minute TTL for run sessions.**
- `agents.claude.cache_ttl: 5m | 1h | cli` (default **`5m`**). `5m` sets `FORCE_PROMPT_CACHING_5M=1` and `1h` sets `ENABLE_PROMPT_CACHING_1H=1` in the session's environment (`_CliAgent.env`). `cli` leaves the CLI's own choice.
- A run's sessions follow each other within minutes, and a session's turns within seconds (§1.3.1). The 1-hour TTL only pays for itself across gaps of 5–60 minutes, and those are rare.
- **The runner checks the effect:** if a session's `usage` shows 1-hour writes while `cache_ttl: 5m` is set, it records a `cache_ttl_ignored` note once per run. The env var is undocumented, so a CLI that ignores it degrades to today's cost and never to a failure.
- *Amended for the C0+C1 plan (doubt review, 2026-11-18):*
  - **The note is generalised** to a `cli_setting_ignored` event, recorded once per setting per run. It covers the TTL in both directions and the auto-memory switch: `init.memory_paths` is not null while `lean` is set.
  - **A refused flag:** a CLI too old for a new flag (it exits with `error: unknown option '--x'` before `init`) stops the step as `blocked`, naming the flag and the fixes, instead of being held as an outage.
  - **The inherited environment** can't override `cache_ttl`: the runner drops the other TTL variables.
  - **The minimum Claude Code version** is 2.1.327.
- **Expected:** −12% to −17% of spend. The −17.4% replay is the upper bound. The 2% of gaps between sessions that are longer than 5 minutes (§1.3.1) take back a little.
- **Decided (Q1):** `5m` is the default. A pack whose sessions sit on tool calls longer than 5 minutes sets `1h`.

**L2b. A lean session prefix.** `agents.claude.lean: true` (default **`true`** for run sessions; interactive use is untouched). Its parts:
- **`--exclude-dynamic-system-prompt-sections`:** moves the cwd, environment, memory paths and git status out of the system prompt, so the system prefix is identical across every run's worktree.
- **`--strict-mcp-config`.** A pack that wants MCP servers in runs names them with `--mcp-config` in `agents.claude.base_args`, and strict mode keeps exactly those.
  - This also closes a gap: a run today loads every MCP server the developer has configured. The probe session showed 7 servers, and the run sessions 13.
  - *Amended for the C0+C1 plan:* no separate `mcp_config` key, since `base_args` already carries one.
- **`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`:** the developer's auto-memory is for interactive work. A run's memory is the lessons file (playbook §15).
- **`--setting-sources project,local`** in every run session, set by a new key `agents.claude.user_settings`, default **`false`** (decided, Q2).
  - **What it drops:** the developer's personal Claude config, meaning their plugins and plugin skills, their MCP servers, their hooks, and their `model`/`modelSettings` (the runner passes `--model` anyway). That cuts ~13K written tokens per fresh session (probe P1/P2).
  - **Why:** it also makes a run independent of whatever is in that config.
  - **What still applies:** the project's `.claude/settings.json` (permissions, hooks), and the runner's `--settings` deny rules.
  - **The cost:** user hooks (notifications, the status line, …) don't fire inside runs.
  - **Escape hatch:** `user_settings: true` for a developer whose runs need a user hook.
- **Skills listing per role.**
  - With the user source dropped, build, plan and debug steps keep native skills (the project's and the engine's), since they may use nested ones (TDD inside build).
  - A step whose brief is self-contained (`role: review`, the sizers, reconcile and record steps) also gets `--disable-slash-commands`, which drops the listing altogether.
  - *Amended for the C0+C1 plan:* C1 applies `--disable-slash-commands` to sizer calls only. A review step whose template names a repository skill loads it by name, so review, reconcile and record steps get it in C3, which reworks their prompts.

**L2c. A run brief in the system prompt.** `context.run_brief: true` (default `true`).
- **What the brief is:** the runner writes `<workspace>/brief/<phase>-<role>.md` and passes it with `--append-system-prompt-file`.
- **What it contains**, in a fixed order, with nothing in it that changes between the sessions of a phase:
  1. the repository's agent rules (CLAUDE.md, or the files in `project.agent_rules`), read from the **main checkout**, with Claude's own auto-loading switched off for the session (`CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`);
  2. the engine preamble and the result contract (`_preamble.md`, `_result.md`);
  3. the role's skill brief;
  4. the run's frozen artifacts for the phase: the approved spec in `plan` and later, and the approved plan in `build` and later.
- **What stays in the user prompt:** only what is specific to the step, such as the task text, the diff range, findings, the gate summary, the lessons and the note.
- **Why it saves:** every build session of a run then reads the spec, the plan and the rules from cache instead of writing them, and the prompt can tell the agent not to `Read` them again (§1.3.3: `handoff.md` 71×, spec ~23× per run).
- **Order and identity matter:** tools come before the system prompt in the cache key. Sessions share the cached brief only when they share a role (same tool set) and a phase (same artifacts). That is why the brief is per `<phase>-<role>`.
- **Trust:** the brief is built from the same sources the prompt reads today (`pack.py` resolution and the main checkout), so `config_trust` and the "never read from a run's worktree" rule apply unchanged.
- **Safety net:** if `CLAUDE_CODE_DISABLE_CLAUDE_MDS` stops working, CLAUDE.md is simply present twice. That costs tokens but changes no behaviour, and the L1 `prefix_write_tokens` signal shows it.
- *Amended for the C2 plan (doubt review, 2026-11-18):*
  - **Item 4 (the approved spec and plan) and L6d are deferred to a later slice.**
    - **Why:** they need their own design for what "approved" means. A runner auto-approval (`needs_approval: false`) emits the same `phase_approved` as a human one. `approval_sha` is taken when approval is *asked for*. The writer step's output and the plan's path have to be found, and a spec can be edited by `record_step` after approval.
    - **Effect on §6:** C2's saving is the rules, preamble, skill and result prefix (L2c items 1–3), plus L3b. The re-reads of the spec and plan that §6 counts wait for that slice.
  - **Trust, corrected.** Today Claude Code loads `CLAUDE.md` from the run's **worktree**, which agents can edit, and the executor's `ctx.repo` is that worktree too.
    - The brief reads the rules, the repository's skill and `brief:` files, and any pack override of `_preamble.md` or `_result.md`, **at the run's fork commit** (`state.base_sha`), with hardened `git cat-file` (`--no-replace-objects`, each blob checked against its id) in the main checkout.
    - That commit is outside anything an agent of the run can change, unlike the worktree or the main checkout's working tree.
  - **Where the brief lives:** in the run's state dir, `<run dir>/brief/<phase>-<role>-<digest12>.md`, not the workspace.
    - The name carries a digest of the content, because steps of one phase and role can name different skills. In `define`, `draft` uses the define skill and `spec_reconcile` the design-doc skill.
    - Each session is **pinned** to the brief it started with, recorded on `agent_started`. Every resume passes the same file after a digest check, whatever the live config now says. A brief that can't be rebuilt to its digest starts a fresh session (`brief_replaced`). A session that started without a brief never gains one.
  - **Rules the brief can't carry fall back to today.** These are `@` imports outside code, a committed `.claude/CLAUDE.md`, `.claude/rules/` or nested `CLAUDE.md`, and an unreadable or oversized file. For them, the rules stay out of the brief and `CLAUDE_CODE_DISABLE_CLAUDE_MDS` is neither set nor inherited, and `validate` warns. Whether the variable also drops organisation-managed memory is not known.
  - **The skill brief** includes the step's configured `brief:` files. Per-task `task_briefs` stay in the prompt.
  - **Size cap:** 150K chars per brief. A brief over the cap, or one that can't be built or written, gives no brief for that session, and the prompt is built in full.
  - **Step-specific lines move to a new `_step_head.md`:** the step id, the result path, the run facts, the code map and the lessons. That leaves `_preamble.md` and `_result.md` run-stable, and they get `Retired` entries. A pack override that holds any placeholder outside the run-stable set turns the brief off for the call.
  - **The brief is Claude only, and never for sizers or the retro.** A Codex step's prompt is built as with `run_brief: false`.
  - **L3b:** a fresh session started from failed gates still gets the whole gate report. Only resume turns get the excerpt.
  - *Measured (probe, 2026-11-18):* a resumed session keeps the appended system prompt it started with. A secret word that was only in the appended prompt came back on `--resume` with the same file, with no file, with a different file and with the variables unset, so `--append-system-prompt-file` is ignored on resume. Pinning still passes the pinned file on every call, which is harmless. Whether `CLAUDE_CODE_DISABLE_CLAUDE_MDS` takes effect on a resume is not verified yet (the canary did not show `CLAUDE.md` in a nested `claude -p`); the real-run check verifies it. Reading the CLI's bundle (2.1.327) also shows that the variable drops organisation-managed memory and user rules, not only `~/.claude/CLAUDE.md` (not probed).

**Expected (L2b + L2c):** the median fresh-session prefix write goes from ~25K to ≈ 6–10K tokens. That saves ~16K × 307 fresh sessions ≈ 4.9M of the 23.3M written tokens: **−8% to −12%**, before the saving on the reads it makes unnecessary.

### 3.3 L3 — Session continuity: resume where it's safe, keep fresh where it's the point

The rule stays: **an author may continue, a reviewer starts fresh.** Independence of review is a quality property (doubt review, per-task review), so L3 never resumes a reviewer into an earlier verdict.

**L3a. Carry the implementer across plan tasks.**
- A new foreach option, `carry: {max_tasks: 4, max_context_pct: 40}`, defaulting to `{max_tasks: 1}`, which is today's behaviour. It generalises `small_plan.shared_session` and `batch`.
- **When it carries:** the next task's implementer **resumes the previous task's session** when all of these hold:
  - the previous task was committed cleanly;
  - its context is under `max_context_pct`;
  - the session has carried fewer than `max_tasks` tasks;
  - the session hasn't rolled over or compacted;
  - the next task isn't sized `risk`.
- **Otherwise it starts fresh,** as it does today.
- **What stays per task:** the reviewer of each task is still fresh, and the commit, gates, guards and attribution are still per task.
- **Why it saves:** each carried task skips the per-session prefix write and the re-exploration (conftest, models and the spec were each read 20–34 times across a run's sessions).
- **Expected:** −3% to −6% of spend, more on plans with many small tasks.
- **Decided (Q7): ships off.** C5 ships the option with the default `{max_tasks: 1}`, which is today's behaviour.
  - The default flips to `{max_tasks: 4, max_context_pct: 40}` in a later release, and only once a real run with carry on shows, in its scorecard, that `gate_first_pass:*` and the review blocking-finding rate didn't get worse.
  - It is the smallest saving and the largest change to session semantics, so evidence comes first.

**L3b. Short resume prompts.** A resume turn sends only what changed:
- the gate failure, cut to the failing tests and the first error lines, with at most `context.resume_gate_lines` lines (default 80);
- the findings;
- the human's note;
- the result reminder.

Today a resume prompt is 10–20K chars (median: implement 10.5K, plan 20K). The session already holds the instructions.

**L3c. Delta-scoped doubt rounds.**
- **Round 1 is unchanged:** a fresh doubt reviewer reviews the whole artifact.
- **Round 2 and later** get a fresh reviewer too, but its prompt is scoped:
  - the artifact's diff since the round it reviewed;
  - the earlier findings, with the reconciler's dispositions;
  - the instruction to check that each disposition holds and to look for new problems **in the changed sections**.
- **Cap:** the round's turn cap is half of round 1's (§3.5).
- **Decided (Q4):** round 2+ always starts a fresh reviewer.
  - Forking the round-1 reviewer (`--resume <id> --fork-session`) would be cheaper, but the reviewer would carry its own earlier verdict, which the doubt design avoids. It isn't built.
  - If C0's signals show doubt round 2+ is still a top cost after L3c, forking comes back as a measured experiment.
- **Unchanged:** the doubt stop rules (`unchanged`, `disputed`, `theater` and `bounded`), the numbering, and the floor that at least one refine round runs.
- **Expected:** together with L4 and L5, −6% to −10% of spend. Spec and plan reviews are 28% of spend, and later rounds are about half of them.
- *Amended for the C3 plan:* the round 2+ turn cap is `max(1, base // 2)`, where `base` is the agent's `max_turns` (unset by default, which means no cap). C4 swaps the base for `max_turns_by_role`.

### 3.4 L4 — Read-only tools for review roles

A `role: review` session doesn't need to edit the tree, run the test suite or call MCP servers. The runner has already run the gates, and the review's job is to read. Today review sessions run with every tool under `bypassPermissions`, and they ran `pytest` 35 times and `Write` 80 times.

**Proposed:** `agents.claude.read_only_roles: [review]` (default `[review]`). For these sessions:

- **Mode and tools:** `--permission-mode dontAsk` (not `bypassPermissions`) and `--tools Read,Grep,Glob,Bash,Write`.
- **Allowed commands:** `--allowedTools` limited to:
  - read-only `git`: `git diff`, `git log`, `git show`, `git status`, `git blame`;
  - Claude Code's built-in read-only commands (`ls`, `cat`, …), which the probe showed run under `dontAsk`;
  - `Edit(//<workspace>/**)`, so the result file can still be written. Under `dontAsk`, anything else is denied without a prompt.
- **Deny rules:** the runner's `--settings` deny rules still apply on top, so the engine floor holds as it does today.
- **What the prompt gets instead:** the run's latest gate results, as a short table of name, pass or fail and duration, so a reviewer has no reason to run tests.
- **Watchdog:** `watchdog.read_only_idle_min` (default 5). Read-only sessions run no long tools, so there's no reason to wait 15–30 minutes.
- **Lean and cheaper:** these sessions also get `--strict-mcp-config` and `--disable-slash-commands` (L2b). Their tool schemas shrink from 37 tools to 5, which cuts ~15K tokens from every turn's cached prefix.

**Verify:** `verify` stays `role: build`, because its job includes fixing what the gates find, and it is 1% of spend. A pack that wants a read-only verify **report** can declare a `role: review` step, and L4 applies to it.

**Defense in depth:** `guards.py` still checks every review step's tree afterwards, as today. L4 makes an edit by a reviewer impossible instead of merely detected.

**Codex:** a `role: review` call uses the existing read-only sandbox, as a fresh sizer does.

*Amended for the C3 plan:*
- **Allow list (Decided 1):** it goes in the `--settings` JSON the runner already passes with its deny rules, not in `--allowedTools`, which avoids a comma-splitting hazard. If the probe shows `--settings` allow rules aren't honoured under `dontAsk`, one comma-joined `--allowedTools` argument right after `--tools` is the fallback, and a workspace path with `,`, `(` or `)` then joins the refused characters of item 6.
- **Not a sandbox (Decided 2):** L4 is a cost and independence control. Under `--setting-sources project,local`, the project's `.claude/settings.json` and `settings.local.json` (a worktree copy an earlier step of the run wrote included), their hooks (a `PreToolUse` hook can allow a call) and, with `user_settings: true`, the user's own rules still apply to a read-only session, and this repository's file allows its `pytest` command. The runner's own allow list holds no test, build or write command, the prompt asks the reviewer not to run them, and the guard catches tracked and untracked edits to the tree.
- **Codex (Decided 3):** unchanged. A Codex review must write its result file, and `-s read-only` would refuse that. `read_only_roles` on a non-Claude agent is a `validate` warning ("has no effect"). The Codex line above moves to a later slice, which needs a result taken from the final message.
- **Idle limit (Decided 5):** this replaces "no reason to wait 15–30 minutes". A read-only session's limit with no tool call open is `min(no_progress_min, read_only_idle_min)` minutes. While a tool call is open it keeps today's `max(idle, gate_timeout_min)`, because item 2 lets a reviewer run a long command. `watchdog.no_progress_min: 0` switches every watchdog off, and `read_only_idle_min: 0` gives read-only sessions today's limits.
- **Workspace paths (Decided 6):** a workspace path the allow rule can't name exactly (it holds one of `*?[]{}\`, and `,()` under the fallback of item 1) makes the step run as today, not read-only, with a `read_only_skipped` note recorded once per run. It is a fallback toward today's behaviour, never a failed review.
- **Guard (Decided 7):** the `read_only_edit` guard compares the tree snapshot (tracked and untracked, not ignored files), as `forbidden_path` does. Ignored paths, `.git/config` and hooks aren't covered, which is the coverage every build step has today. The sizer's strict `git_state` + `tree_state` check would fail every test run item 2 allows (`__pycache__`, `.pytest_cache`).
- **Denials (Decided 8):** a denial in a read-only session is recorded as `permission_denied` with `read_only: true`; `signals.py` leaves it out of `permission_denied:<tool>`, and a `blocked` stop's report says the denial is by design.

### 3.5 L5 — Turn caps, spend caps and the idle limit

**L5a. A turn cap per role, with one wrap-up turn.**
- **Defaults:** `agents.claude.max_turns_by_role: {review: 30, plan: 60, build: 60, debug: 60, mechanical: 15}`. These are roughly 1.5× the p90 measured in §1.2, so they cut the tail (84 and 89 turns) and not the typical session.
- **Overrides:** a step can set its own `max_turns:`, and doubt rounds after the first get half of round 1's cap (L3c).
- **Wrap-up:** when Claude ends with `error_max_turns`, the runner resumes the session **once** with a fixed wrap-up prompt and `--max-turns 3`: "You have used the turn budget. Write your result now with what you have; list what is left as `todo`." If there is still no result, the run stops as `turn_cap`. The resolver may take that stop, with the human's options: raise the cap once, split the step, or descope.
- **Spirals:** the `session_turns_p90:<kind>` signal (L1) shows a step kind that keeps spiralling, so a lesson can target it.

**L5b. A spend cap per session** (the "action cap" of harness §3.11, built narrow). Decided (Q3): **on by default, as a backstop.**
- **Key:** `agents.claude.session_budget_usd: {review: 4, plan: 10, build: 10, debug: 10}`, passed as `--max-budget-usd`.
- **Sizing:** the defaults are set high so that a normal session never reaches them. C0 checks them against the measured per-session p99 (target: about 3× p99), and the C0+C1 PR adjusts them if needed.
- **How the flag behaves** (probes B1 and B2):
  - it counts list-price dollars on a subscription account too;
  - it ends the session after the turn that crosses the cap, so it overshoots by one turn;
  - the session exits 1 with `error_max_budget_usd` and no result text;
  - the cap counts per invocation, so a resume gets a fresh cap.
- **What the runner does at the cap:** the same as at the turn cap (L5a). It resumes once with the wrap-up prompt under its own small cap. With still no result, the run stops as `budget_cap`, with the same human options.
- **No per-step cap** (decided, Q6). One step can spend up to about (1 + `fix_attempts` + wrap-up) × the cap. That range is already bounded by `fix_attempts`, the turn caps and `run_budget_hard_usd`. L1's per-step cost signal shows whether a step-level cap is ever needed.

**L5c. The idle limit** stays as T3 shipped it (15 minutes), plus `read_only_idle_min` (L4). The data shows no case for changing the default: 18 gaps over 15 minutes in 5,547 calls.
- *Amended for the C3 plan:* `read_only_idle_min` applies only while no tool call is open (L4, Decided 5), and `0` gives read-only sessions today's limits.

**L5d. Compaction.** 102 auto-compactions at ~110K tokens is a symptom of context growth, not a policy to tune first. L2c, L3 and L6 shrink the context. L1's `compactions` signal shows whether `context.autocompact_pct` needs to change afterwards. No default changes in this proposal.

### 3.6 L6 — Context hygiene: less tool output into the context

**L6a. Cap the shell output.** `agents.claude.bash_output_chars` (default 15000) sets `BASH_MAX_OUTPUT_LENGTH`. There were 128 tool results over 20K chars, and long test or grep output is the main cause.

**L6b. Targeted checks in the build prompt.** The engine's build prompts and the bundled `agentflow-build` skill say three things:
- run only the tests of the files you changed, with quiet flags;
- the runner runs the gate sets after you;
- don't run the full suite.

Implementers ran `pytest` 262 times and lint 331 times in their sessions. The gate summary the runner feeds back on a failure is already the authoritative check. Packs can add a project-specific test command hint through `.agentflow/prompts/`.

**L6c. Search with the tools, not the shell.** The preamble asks for `Grep` and `Glob` with limits instead of `grep -r` and `find` through Bash (the "search" category: 1.2M chars in implement alone), and for reading a file once (re-reads: 28–38%). These are prompt changes. Any reworded engine prompt text gets a `Retired` entry in `pack_fix.py`, as CLAUDE.md requires.

**L6d. Files in the brief aren't read again.** With L2c, the prompt states which artifacts are already in the system prompt: spec, plan and rules.

**Expected (L6):** −5% to −10% of spend, by shrinking the context that every later turn reads.

### 3.7 L7 — The usage-window governor: finish features, don't start them

**Mechanism:** the runner reads `usage_window` (L1) before it starts any agent session. `usage.governor: on | observe | off` (default **`on`**), with these keys:

| Key | Default | What it does |
|---|---|---|
| `usage.five_hour_soft` | 0.75 | Above it, start **no new run** (queue, Backlog or `next`), no sizer for a run that hasn't started, and no new phase of a run whose current phase is done. Steps inside a phase that is already running continue. |
| `usage.five_hour_hard` | 0.92 | Above it, start no new session at all. Hold every run (`run_held`, `reason: usage window`) until `resetsAt`, instead of starting a 30-turn session that dies at 100% and needs a cold resume after the reset. |
| `usage.seven_day_soft` | 0.85 | The same as `five_hour_soft`, against the weekly window. |
| `usage.finish_first` | `true` | When several runs wait on the governor, the one closest to `finish` goes first. The order is the phase index, then the fewest open tasks. |

**Details:**
- `observe` records `usage_governed` events (what would have been held) without holding anything.
- The governor reads the newest utilization any session on the machine has reported, through a small file the runner keeps in the user state dir (`safeio`). One run's stream therefore protects every run, and the dashboard header can show the window.
- This is a scheduling rule over a signal the runner already receives. It's a harness decision, never a model decision.
- **Interactive use** (decided, Q5). The developer's own Claude Code sessions draw on the same window, and the governor never acts on them. It only warns: the dashboard header shows the window, and notes when utilization rose faster than the runs' own spend explains.
- **Headroom** (decided, Q5). The soft mark is also the developer's reserved headroom. At the default 0.75, runs stop starting new work with a quarter of the window still free. `usage.five_hour_soft` in the user layer changes it per developer.

**Expected:** the governor doesn't lower tokens much. It does change the outcome: features finish inside a window instead of five of them stopping half-way, and the cold resumes after a hold (full-context rewrites) stop.

---

## 4. New and changed config keys

*Amended for the C0+C1 plan:* `agents.claude.mcp_config` was dropped (see L2b).

Every key ships with the four things CLAUDE.md requires: a Settings form field or a `_PROTECTED_KEYS` entry, `validate` checks, a `test_config_edit.py` case, and a playbook §14 line.

| Key | Default | Notes |
|---|---|---|
| `agents.claude.cache_ttl` | `5m` | `5m \| 1h \| cli`; Settings form field |
| `agents.claude.lean` | `true` | run sessions only; Settings form field |
| `agents.claude.user_settings` | `false` | `false` passes `--setting-sources project,local`; a personal key (`agents.claude.user_settings` in `layers.USER_KEYS`); Settings form field (Claude section only). `doctor` warns when the user's Claude settings hold `env`, `apiKeyHelper` or `permissions`, which runs would then skip. `validate` warns about a project `.mcp.json` under `lean`, and about an `overlays.security.review_agent` that isn't in `.claude/agents/`. |
| `agents.claude.read_only_roles` | `[review]` | `validate`: only known roles; Settings form field |
| `agents.claude.max_turns_by_role` | `{review: 30, plan: 60, build: 60, debug: 60, mechanical: 15}` | `agents.<name>.max_turns` stays as the fallback |
| `agents.claude.session_budget_usd` | `{review: 4, plan: 10, build: 10, debug: 10}` | `{role: usd}`; `{}` turns it off; checked against C0's p99 |
| `agents.claude.bash_output_chars` | `15000` | `0` leaves the CLI's default |
| `context.run_brief` | `true` | |
| `context.resume_gate_lines` | `80` | |
| `project.agent_rules` | `[CLAUDE.md]` | files that make up the brief's rules section; read from the main checkout |
| `phases.<p>.steps[].carry` | `{max_tasks: 1}` (off) | on a `foreach: plan_task` step; `validate`: `max_context_pct` between 10 and 70; the default flips only on evidence (Q7) |
| `phases.<p>.steps[].max_turns` | unset | step override |
| `watchdog.read_only_idle_min` | `5` | |
| `usage.governor` | `on` | `on \| observe \| off` |
| `usage.five_hour_soft` / `five_hour_hard` / `seven_day_soft` | `0.75` / `0.92` / `0.85` | `validate`: 0 < soft < hard ≤ 1 |
| `usage.finish_first` | `true` | |

`usage.*` are personal keys as well as pack keys: the window belongs to the developer's account, not to the project. They join `layers.USER_KEYS`, so the user file can set them for every project at once.

## 4b. Pack-side actions (no engine change)

Decided (Q8). The cheapest cut available today needs no engine work. T3's route profiles skip steps and cap doubt rounds, and doubt reviews are 28% of spend (§1.2). They're off by default, and the `customers-support-agentic-ai` pack doesn't turn them on. That pack is recommended to:

1. set `route.mode: observe` now, so each run records the profile the triage would pick (`route_profile`) and changes nothing;
2. switch to `route.mode: on` once a few runs show those profiles fit the work.

Two things stay as they are:
- the engine default stays `off`, as T3 decided;
- this remains each pack's own choice.

## 5. What stays exactly as it is

- **Steps and gates:** the steps of every lane, their order and their conditions. The gate sets, the fix loop and the gates at `finish`.
- **The engine floor:** a non-empty `gate_sets.verify`; `.agentflow/**` always forbidden; user-only skills refused; at least one automatic refine round for `critical`, `high` and `medium` findings.
- **Review independence:** every reviewer starts in a fresh session, and the doubt review's stop rules and numbering are unchanged.
- **Commits:** only the runner commits and pushes, per task, with guards over every step's tree.
- **Obligations and conformance at `finish`.**
- **The golden `explain` files:** byte-identical. `explain` shows no new step, and the session flags aren't part of its output.

## 6. Expected effect (estimate)

The base is the 5 features of §1, at T3's Sonnet defaults: **≈ $160, or $32 per feature**. The levers act on overlapping tokens, so their effects multiply rather than add.

| Lever | Acts on | Estimate |
|---|---|---|
| L2a 5-minute TTL | every cache write | −12% to −17% |
| L2b + L2c lean prefix (incl. no user settings) and run brief | ~4.9M of 23.3M written tokens, plus re-reads of the spec, plan and rules | −8% to −12% |
| L3c + L4 + L5a scoped doubt rounds, read-only reviews, turn caps | review-type sessions (38%) | −6% to −10% |
| L6 context hygiene | tool output (Read, search, in-session tests) | −5% to −10% |
| **Combined, engine defaults** | | **≈ −28% to −42%**, so ≈ $19–23 per feature and **1.4–1.7× more features per usage window** |
| L3a carry the implementer | implement sessions (21%); ships off (Q7) | a further −3% to −6% once its default flips |
| §4b `route.mode: on` (pack-side) | skipped steps and capped doubt rounds | not estimated here; T3's own measure |
| L7 governor | scheduling | features finish within a window; no cold resumes after a hold |

These are estimates from one day of five runs on one pack. Slice C0 gives the measured baseline. Each later slice is judged by `agentflow scorecard` against it, and a lever that doesn't pay is reverted or its default turned off.

## 7. Rollout (each slice is one testable PR, feature lane on agentflow itself)

| Slice | Content | Acceptance |
|---|---|---|
| **C0 + C1 Measure and cache** (one PR, decided Q9) | L1: `agent_exited.usage`, `usage_window`, signals, the Metrics panel. L2a `cache_ttl`, L2b `lean` (incl. `user_settings: false`, auto-memory off, the skills listing dropped for sizers), the `cli_setting_ignored` note and the refused-flag stop. | Fake-agent usage lands exactly in metrics and signals. The `argv`/`env` unit tests show the flags per role. One real run shows `ephemeral_5m` writes and a lower `prefix_write_tokens` than §1's baseline. Golden files unchanged. |
| **C2 Brief** | L2c run brief, `project.agent_rules`, L3b short resume prompts, L6d | Two sessions of the same phase and role build a byte-identical brief file (test); the prompt no longer repeats it; `pack_fix` `Retired` entries for reworded prompts |
| **C3 Reviews** | L4 read-only roles (`dontAsk` + allowlist + workspace write), the gate-result table in review prompts, `read_only_idle_min`, L3c delta rounds | A fake review that tries `Edit` or `pytest` sees a denial; guards still catch a forced edit; the doubt tests (stop rules, numbering) pass unchanged |
| **C4 Caps** | L5a `max_turns_by_role`, the wrap-up turn and the `turn_cap` stop; L5b `session_budget_usd` (on, as a backstop) and the `budget_cap` stop; L6a–c | A fake session ending with `error_max_turns`, or with `error_max_budget_usd` and no result, gets one wrap-up and then `turn_cap` or `budget_cap`; the resolver's options are listed |
| **C5 Carry** | L3a `carry` on `foreach: plan_task`, default off | With `carry: {max_tasks: 4}`, a fake 4-task plan resumes one implementer session for tasks 1–3 and goes fresh at the context limit; reviewers stay fresh; commits stay per task; the default run is unchanged |
| **C6 Governor** | L7 `usage.*`, the shared window file, finish-first ordering, dashboard header | Fake `rate_limit_event`s at 0.8 and 0.95 hold new runs and new sessions respectively; `observe` only records |

**Order:** C0+C1 → C2 → C3 → C4 → C6 → C5. C1 and C2 are the cheapest and largest wins. C5 changes session semantics the most, so it goes last, after the signals have shown the earlier slices' effect.

**Baseline:** the runs from before C0 have no usage events, so the new signals (`cache_write_share`, `prefix_write_tokens`, `session_turns_p90:*`) start from the numbers in §1.2–§1.3, measured by replaying transcripts. The scorecard's existing signals freeze as usual.

**Security overlay:** C0+C1, C3 and C4 change `agents.py`, which triggers the pack's security overlay (`overlays.security.when_paths`). They are planned and reviewed with `security-and-hardening` and the `security-auditor` agent, with particular attention to the `dontAsk` allowlist, the workspace write rule and `--strict-mcp-config`.

## 8. Risks

| Risk | Mitigation |
|---|---|
| The undocumented env vars (`FORCE_PROMPT_CACHING_5M`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY`, `BASH_MAX_OUTPUT_LENGTH`) change or disappear in a CLI update | Each one fails toward today's behaviour: higher cost, never wrong behaviour. The runner detects an ignored TTL or auto-memory switch from the stream (`cli_setting_ignored`), stops on a refused flag, and the L1 signals show a prefix regression. |
| A 5-minute TTL costs more on a pack whose sessions sit on long tool calls (> 5 min) | `cache_ttl: 1h` per pack. The L1 signals show it, and the C1 replay script can be run on any pack's history. |
| Read-only reviewers miss something they would have found by running tests | The gates have already run, and their results are in the prompt. `branch_review` and the human review are unchanged. Watch the blocking-finding rate in the scorecard. |
| A carried implementer session drifts or anchors on an earlier task | Carry only below 40% context, never across a `risk` task, never after a compaction; a fresh reviewer per task. Off (`max_tasks: 1`) at a single key's cost. |
| Turn caps cut a legitimately long session | The caps are ~1.5× p90, and a wrap-up turn comes before the stop. `turn_cap` is a named stop with "raise once" as an option. |
| The governor idles a machine that has budget left in another window or on another account | `usage.governor: observe`, or per-user thresholds in the user layer. |
| A developer relies on a user hook or plugin inside runs (with `user_settings: false` it no longer loads) | `agents.claude.user_settings: true` in the user layer; `doctor` lists the user hooks and plugins a run won't load |
| The spend cap overshoots by one turn and ends with no result | It's a backstop set high; the wrap-up resume gets its own cap, and `budget_cap` is a named stop |
| Moving CLAUDE.md into the brief changes how the agent weighs it (system prompt vs user context) | C2 compares a real run's `gate_first_pass` and review findings against C1's; fall back with `context.run_brief: false`. |

## 9. Testing strategy

- **Unit tests** (`test_agents.py`): `ClaudeAgent.argv`/`env` per role and per key, covering the lean flags, `dontAsk` and the allowlist for read-only roles, `--append-system-prompt-file`, the TTL env and `--max-turns` per role. `parse_claude_line` folds `model_usage_since_last_result`, `rate_limit_event` utilization and `error_max_turns`.
- **Fake agent:** `fake_agent.py` learns to emit usage splits, rate-limit events and `error_max_turns`. Flow tests cover the wrap-up turn, the `turn_cap` stop, carry across tasks, the governor's holds and delta-scoped doubt round prompts.
- **Config:** `test_config_edit.py` gets a case per key; `validate` gets its checks; `test_portability.py` stays green (no project strings: the brief's rules come from `project.agent_rules`).
- **Golden files:** the `explain` fixtures stay byte-identical.
- **Real-run check per slice:** one feature run on agentflow itself, with its scorecard against the C0 baseline in the PR description.

## Decided (grilling, 2026-11-18)

| # | Decision | Where |
|---|---|---|
| Q1 | The 5-minute cache is the default. 2% of gaps between sessions are over 5 minutes (median 0 s across the machine), and 0.8% within a session. A pack can set `1h`. | §1.3.1, L2a |
| Q2 | Every run session drops the user setting source (`--setting-sources project,local`): no user plugins, MCP servers or hooks in runs. `agents.claude.user_settings: true` is the escape hatch. | L2b, §4 |
| Q3 | The per-session spend cap is on by default, as a high backstop: `{review: 4, plan: 10, build: 10, debug: 10}` USD, checked against C0's p99. It counts per invocation; hitting it gets one wrap-up resume, then a `budget_cap` stop. | L5b, §4 |
| Q4 | Doubt round 2+ uses a fresh reviewer with a delta prompt. Forking the round-1 reviewer isn't built. | L3c |
| Q5 | The governor only warns about interactive use, and its soft mark (0.75) doubles as the developer's reserved headroom. | L7 |
| Q6 | No per-step spend cap. The per-session cap, `fix_attempts`, the turn caps and the run's hard budget bound it. | L5b |
| Q7 | Implementer carry-over ships off. Its default flips only after a scorecard shows no quality loss. | L3a, §6 |
| Q8 | Pack-side: the customers pack turns `route.mode` to `observe`, then to `on`. The engine default stays `off`. | §4b |
| Q9 | C0 and C1 ship as one PR. The new signals' baseline is §1's replay numbers. | §7 |

## Open Questions

None.

## Verification

- `agentflow scorecard` after each slice, against the C0 baseline, shows:
  - `cache_write_share` ≤ 35% and `prefix_write_tokens` ≤ 10K (after C2);
  - list-price spend per finished feature run down ≥ 30% (after C4);
  - conformance at 100%, `gate_first_pass:*` and the blocking-finding rate not worse.
- A day of parallel runs with `usage.governor: on` ends with no run held mid-phase by the usage window, and every started feature either finished or waiting at a phase boundary.
- The full suite, `uvx ruff@0.17.4 check .` and `bin/agentflow validate` (0 warnings) pass on each slice; the golden `explain` files are byte-identical.
