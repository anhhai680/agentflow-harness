# Proposal: agentflow dashboard — from a board of cards to a run cockpit

Status: approved (2026-11-15)

Prototype: [agentflow-dashboard-ux-prototype.html](agentflow-dashboard-ux-prototype.html) (open it in a browser; start with **Tour** in the top bar), a clickable mock built from issue-81's real data.

Related: `docs/agentflow-harness-proposal.md` (§3.3 the Route view, §3.9 Settings), `docs/specs/agentflow-throughput-proposal.md` (parking, blast radius, batched approvals and parallel workers all need a UI), `docs/plans/2026-11-09-agentflow-harness-dashboard-dialog-errors.md`.

## Context

A developer supervising agentflow does three jobs:

1. **Manage the agents** of each run: what is running now, what ran before, what it cost, why it stopped, and what it said.
2. **Follow the process**: where the run is, which round of which step, which plan task, what is blocked and by what.
3. **Judge what agentflow wrote**: the **spec** from `define` and the **plan** from `plan`, which are the two documents a human is asked to approve.

The dashboard was built as a Kanban board, and it shows *where* a run is very well. It doesn't support any of the three jobs. You can't read the spec or the plan in it at all. A run's agents disappear from it 10 minutes after they exit. The run's process is a plain-text dump inside a side drawer that rebuilds itself, and collapses, several times a second while an agent works. So the human leaves the dashboard for the terminal, `~/.local/state/agentflow/<pack>/runs/<run>/…`, the worktree and an editor. That is exactly the attention cost agentflow exists to remove.

This proposal turns the run drawer into a **Run page** with first-class Spec, Plan, Agents and Activity views. It turns "Needs human" into an **Inbox of decisions**, and it makes live updates stop fighting the reader. It adds no dependency (the engine stays stdlib + `pyyaml`), no build step and no framework, and it keeps the dashboard's security model as it is.

---

## 1. Evidence

Method: reading `agentflow/dashboard.py`, `agentflow/dashboard.html` and `agentflow/views.py` at `85860bb`, and checking them against the run directories of the `customers-support-agentic-ai` pack (25 runs, 8.6 MB of `events.jsonl`). The worked example is issue-81: 24 plan tasks, 127 agent sessions, 5,232 events.

### 1.1 The three jobs, and where the dashboard leaves the human

| Job | What the dashboard offers today | Where it fails |
|---|---|---|
| Read the spec before approving `define` | The inbox text: "Phase `define` is done. Review it", and a path, `docs/specs/SPEC-issue-81.md` | The spec (191 lines) is never shown. It lives in the run's worktree under the state dir. The approver has to find that path and open an editor |
| Read the plan before approving `plan` | The same: a path to a 678-line, 24-task plan | No render, no task list, no `Depends on:` graph, no view of what refine round 2 changed |
| See why a review sent a phase back | "Review findings" as `JSON.stringify` (`dashboard.html:771`) | Findings carry `file` + `line`, but nothing links them to the document they point into |
| See the agents of a run | The card shows `live[-1]`, the newest live agent only (`views.py:233`). The Agents tab shows live agents plus those that exited in the last 10 min (`DEFAULT_EXITED_MIN`) | For a finished or waiting run that is **0 of 127 sessions**. Retries (`task-12/implement.1`, `.2`), resolvers and sizers can't be told apart |
| Read what an agent did | "Transcript tail": the last 16 KB of the **newest** `transcripts/*.jsonl`, as raw JSON lines (`views.py:608`, `:622`) | issue-81 has 383 transcript files; the others can't be reached. The raw lines are mostly `tool_progress` and `system` records |
| Follow the run's history | "Timeline": the last 300 events (`TIMELINE_MAX`), each as `key=value` text (`describe()`, `dashboard.html:723`) | That's **6 %** of issue-81's 5,232 events. 73 % of the log is `agent_turn`, so the 300 shown are mostly noise |
| Read the handoff | The last 16 KB of `handoff.md` (`TAIL_BYTES`) | `handoff.md` is written **newest phase first** (`pipeline.py:637`) and is 65 KB, so the tail keeps the *oldest* phases and drops the newest. `pr.md` (41 KB), `issue.md`, `refine/*.md` and `handoffs/<phase>.md` aren't shown at all |
| Decide on an inbox item | A row reading `<run-id> · build/build/task-9/implement/resolve-2 · blocked · 9 h 12 min ago`. Approve uses `prompt()`/`confirm()` (`dashboard.html:637-644`) | Raw keys, and no context next to the decision. A browser `prompt()` holds a one-line note for a stop that needs an explanation |

### 1.2 The page fights the reader

- **Every event rebuilds everything.** `stream.onmessage` schedules a refresh after a 250 ms debounce (`dashboard.html:1506`). The refresh refetches `/api/board` and `/api/agents`, then calls `openDrawer()`, which rebuilds the drawer from scratch with `replaceChildren` (`:1493`). Every `agent_turn` triggers this. While an agent works, the sections you expanded collapse and the scroll position resets, up to 4 times a second.
- **Each refresh is heavy.** `board()` reads every run's full event log (`views.py:288-304`): 8.6 MB for this pack. `run_detail()` ships the whole `state.json` to the page (`"state": state`, `views.py:613`), and that file is 267 KB for issue-81.
- **Buttons ignore the run's state.** The drawer shows Pause, Resume, Retry failed step and Cancel on every run, whatever its state (`dashboard.html:749-754`). A wrong press answers 409, and the user learns the rule from the error.
- **No address for anything.** The drawer has no URL. A reload, or sharing "look at task 9 of issue-81" with yourself in another tab, loses the place.
- **The drawer is 760 px wide** (`#drawer`). Every section in it is a collapsed `<details>` holding a `<pre>`: the wrong shape for a 678-line plan.

### 1.3 What already exists and can be reused

Almost everything this proposal shows is already recorded. The UI simply doesn't read it.

| Needed | Already there |
|---|---|
| Which files a phase produced (spec, plan) | `pipeline._artifacts`: `changed_since(worktree, phases.<p>.start_sha)` |
| Each revision of the spec or plan | the `sha` on `step_finished`/`step_committed` for `define/draft`, `define/spec_reconcile`, `plan/plan` and each refine round |
| Review findings anchored in the document | `step_findings`, `review_result` and `state.reviews` carry `{severity, file, line, text}` |
| Why a phase was sent back | `refine/<phase>-<n>.md` |
| The plan's tasks and dependencies | `sources.py`: `_depends_on` (D12) and `task_files` |
| Each task's progress | `state.steps["build/build/task-N/{implement,review,…}"]`, `task_committed`, `state.reviews` |
| Every agent session | `agent_started`/`agent_session`/`agent_exited`, `state.exited` (127 rows), `session_costs`, `transcripts/<key>.<attempt>.jsonl` |
| What the human decided, and when | `hitl_*`, `phase_*`, `checkpoint_cleared` events |

The worktrees share the main repository's object store. So `git show <sha>:<path>` still reads any revision of the spec or plan after the worktree is gone, as long as the commit is reachable.

---

## 2. Goals and non-goals

| KPI | Today | Target |
|---|---|---|
| Steps to read the spec or plan under approval | leave the UI, find the worktree, open an editor | **1 click** from the inbox item |
| Approvals of `define`/`plan` where the approver opened the artifact (`viewed` on the approval event, §6) | not measurable, and impossible in the UI | **≥ 90 %** |
| A run's agent sessions reachable in the UI | live + last 10 min (0 of 127 for issue-81 now) | **all**, each with a readable transcript |
| A run's events reachable in the UI | last 300 (6 % for issue-81) | **all**, filtered and paginated |
| UI state lost to live updates (open sections, scroll, tab) | on every event | **never** |
| Refreshes per second while agents run | up to 4, each a full board + full `state.json` | **≤ 0.5**, scoped to the run that changed |
| Actions answered 409 because the button didn't apply | possible on every drawer action | only real races |
| Touches to clear one inbox item | 1–8+ (issue-81 task 9 took 8 after the answer) | **1–2**, with the context on the same screen |

**Non-goals:**
- Editing the spec or plan in the browser. A human edit goes through `reject` with a note, or through git, as today. See Open Question 2.
- A JavaScript framework, a bundler or a CDN. The CSP stays `script-src 'nonce-…'`, and `pyproject.toml` keeps one dependency.
- A multi-user or remote dashboard. It stays `127.0.0.1`, single user, token-gated.
- Changing what the runner does. Every view here reads state the runner already records. The only engine additions are read-side helpers and one optional field on approval events.

---

## 3. Design principles

1. **The document under decision sits next to the decision.** An approval button never appears without the thing it approves one click away, or already on the screen.
2. **Every object has an address.** A run, a tab, a task, a session, a finding: `#/run/<id>/plan/task-9`. Reload and back/forward work.
3. **Humans read labels, not keys.** `build/build/task-9/implement/resolve-2` is shown as **Task 9 · implement · resolver 2**. The key stays available as a copyable detail.
4. **Live, but still.** Updates patch the region that changed. They never reset what the reader opened, scrolled or typed.
5. **Show only actions the state allows.** The server sends, with each run, the actions valid now. The page renders only those.
6. **Same evidence as the CLI.** Every view is a projection of `events.jsonl`, state and git, so the CLI, the PR body and the page never disagree.

---

## 4. Design

### 4.1 Information architecture

```
 Header:  agentflow v2.7 │ Inbox (3) │ Board │ Agents │ Workflow │ Metrics │ Lessons │ Settings │ ● live
                             │           │        │
                             │           │        └─ every live agent, grouped by run (4.8)
                             │           └─ cards; a click opens the Run page; Space peeks (4.7)
                             └─ decisions across runs, each with its document (4.6)

 Run page  #/run/<id>/<tab>
   Overview │ Spec │ Plan (24) │ Agents (127) │ Activity │ Gates │ Files
```

The drawer stays as a **peek**: the card's facts and the decision waiting on it. It gains an **Open run** button. The full surface moves to the Run page, which uses the whole viewport.

### 4.2 The Run page: header and stage rail

```
┌ ← Board   issue-81 · Conversations phase 2: own conversations by customer key   [feature] ───┐
│ ⛔ Waiting on you 9 h 11 min — Task 9 stopped by floor_guard; blocks 1 task (21)  [Open decision]│
│ $40.72 of $90 · 11 h 58 min wall (agents 2 h 18 min · you 9 h 11 min)       branch ⧉  PR ↗   │
│                                                                                              │
│  define ✓ 38m ── plan ✓ 1h ── build ◆ 8/24 ── verify ○ ── review ○ ── ship ○                 │
│  3 doubt rounds    2 refine     1 blocked · 1 waiting on it · 13 idle                        │
│                                                         [Pause]  [Cancel run…]   ⋯ attach   │
├ Overview │ Spec │ Plan 24 │ Agents 62 │ Activity │ Gates │ Files ──────────────────────────────┤
```

- **The stage rail** is the run's phases from `run_workflow()`. Each phase carries its status, wall time (`metrics.phases`) and one line of what is notable: rounds, refines, parked or blocked tasks. A click opens that phase in the Overview tab.
- **The banner** appears only when the run waits on a human. It names the decision in words and links to it (4.6).
- **Actions** come from a new `actions` list on `/api/runs/<id>` (§4.10), so Pause shows only while the run can pause, and so on. Cancel opens a confirmation dialog that names what will be stopped ("2 live agents: Task 10 implement, Task 8 review").
- **Overview tab:** the phase list with each step's status, rounds, attempts, duration and cost. These are today's `runWorkflow()` plus the `metrics` the drawer already computes, laid out as one table instead of nested `<ul>`s. Then the risks and open findings from the handoff, and the newest 10 human-readable activity lines.

### 4.3 Spec and Plan tabs: the artifact viewer

This is the heart of the proposal: what agentflow wrote, shown where the human decides on it.

```
┌ Spec ─────────────────────────────────────────────────────────────────────────────────────────┐
│ docs/specs/SPEC-issue-81.md   revision: [draft] [reconcile r1] [reconcile r2 ●] [approved]     │
│                               [Diff with previous ▾]   [Copy path]   191 lines                 │
├──────────────────────────────────────────────────────┬────────────────────────────────────────┤
│  32 ## Scope                                          │ Doubt review (define/spec_review)       │
│  33                                                   │  Round 2 · 4 findings                   │
│  34 ● 3. simulator_conversations, holding, the picker │  ● high   L34  Scope 3 says … the code  │
│       and the routes are otherwise unchanged …        │           contradicts this …  [open]    │
│  …                                                    │  ● med    L65  repeat loop takes new    │
│  65 ● …                                               │           key locks after row locks…    │
│                                                       │  ○ low    L120 … (resolved in r2)       │
├──────────────────────────────────────────────────────┴────────────────────────────────────────┤
│ Waiting on you: approve define        [Approve spec]   [Request changes (2 comments)]          │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
```

- **Which files.** The server lists a run's artifacts from what the runner already knows: `changed_since(start_sha)` for each phase, plus the `sha` of each `step_finished` that touched them. It classifies each one by the phase that produced it (`define` → spec, `plan` → plan, others → files). That keeps the engine project-agnostic: there is no `docs/specs` string in the engine. The Spec and Plan tabs appear only when the lane walks those phases.
- **Which revision.** Each step that rewrote the document (draft, grilling applies, reconcile rounds, refine rounds) is one revision chip. The default is the newest. A finished run also shows **as merged**.
- **Diff with previous.** This is a `git diff <from> <to> -- <path>`, rendered by line with the existing `.d-add`/`.d-del` classes. It answers "what did refine round 2 change?", today answerable only with git.
- **Findings in the gutter.** Each `step_findings`/`review_result` finding whose `file` is this document gets a dot on its `line`, coloured by severity. The side panel lists them by round, open findings first. A finding about another file links to the Files tab.
- **Rendering.** A small safe Markdown renderer: headings, lists, tables, fenced code, emphasis and inline code. It is about 200 lines, builds DOM nodes and sets only `textContent`, never `innerHTML`, which keeps today's rule (`dashboard.html:260`). Links render only for `http(s):` and repo-relative targets, with `rel="noopener noreferrer"`. A **Raw** toggle shows the numbered source.
- **Request changes.** You select lines and add a comment. The comments compose into one reject note (`L34: …`, `L65: …`) shown for confirmation, then sent through today's `POST /api/runs/<id>/reject`. The refine round gets line-anchored feedback without any engine change.
- **Approve** sends today's approve call, plus `viewed: [{path, sha}]` for the revisions the page displayed (§6).

Other documents get the same viewer, in **Files**: `issue.md` (the original request), `handoffs/<phase>.md` (one per phase, which fixes the truncated, newest-dropped `handoff.md`), `pr.md`, `refine/<phase>-<n>.md` and `progress.md`. Files also lists the run's changed files since `base_sha` with a diff per file. That's the PR diff, readable before a PR exists.

### 4.4 Plan tab: the plan as a task board

The plan is both a document and the build phase's work list. The Plan tab has two modes: **Document** (the viewer of 4.3) and **Tasks** (the default once `build` starts).

```
┌ Plan · 24 tasks · 8 committed · 1 blocked · 1 waiting on it · 13 independent · 1 deferred      ┐
│ Filter: [all ▾] [blocked/parked] [has findings]     Group: [by status | by dependency wave]    │
├────┬──────────────────────────────────────────┬─────────┬───────────────┬─────────┬──────┬─────┤
│  # │ Task                                     │ Depends │ Status        │ Impl    │ Rev  │ $   │
├────┼──────────────────────────────────────────┼─────────┼───────────────┼─────────┼──────┼─────┤
│  1 │ Migration 0018: customer_key columns     │ —       │ ✓ committed ⧉ │ 1 try   │ ✓ 0  │ 1.1 │
│  8 │ Ownership by customer_key in reads       │ 3, 5    │ ✓ committed ⧉ │ 2 tries │ ✓ 1m │ 2.4 │
│  9 │ Conversations tests use customer_key     │ 8       │ ⛔ blocked     │ 7 tries │ —    │ 6.3 │
│    │   floor_guard: rename-only assertion in test_kpi.py · resolver 2 failed · [decision]     │
│    │   blocks: 21 · independent: 10–20, 22, 24                                               │
│ 10 │ Other modules' test call sites           │ 8       │ ◇ independent │ —       │ —    │ —   │
│ 21 │ …                                        │ …, 9    │ ○ waiting on 9│ —       │ —    │ —   │
└────┴──────────────────────────────────────────┴─────────┴───────────────┴─────────┴──────┴─────┘
```

- **Rows** are the plan's tasks as `sources.py` parses them: title, `Depends on:` and files. Each is joined with its step records: implement and review status, attempts, rollovers, resolvers, the review's blocking and minor counts, cost, and the `task_committed` sha.
- **Expanding a row** shows the task's text from the plan, its declared files, its sessions (links into the Agents tab), its gates and its findings.
- **Blocked and parked tasks** show their blast radius. It is computed from the same `Depends on:` closure that `feature_lane.may_park` uses, so the human sees what a stop is really holding up. This is the UI half of the throughput proposal's §4.1.1. In issue-81, the real plan shows that only Task 21 depended on Task 9, yet tasks 10–20, 22 and 24 all sat idle overnight. Nothing on today's dashboard could show that.
- **Group by wave** shows the DAG as topological batches. Today that is a reading aid. Once the throughput proposal's parallel build (T5) lands, it becomes the live view of what runs together.

### 4.5 Agents tab (per run) and the transcript viewer

```
┌ Agents · 127 sessions · 2 live · $41.20 ──────────── Filter: [phase ▾] [role ▾] [failed only] ┐
│ ● live  Task 10 · implement      sonnet · high ↑   12 min   ctx 41 %   $0.62   Edit 2 s ago     │
│ ● live  Task 8  · review         sonnet · medium    3 min   ctx 18 %   $0.11   Read 9 s ago     │
│ ─ build ───────────────────────────────────────────────────────────────────────────────────────│
│ ✗ exit 1  Task 9 · implement · resolver 2   opus · high   31 min  $1.90  "floor_guard fails …"  │
│ ✓ exit 0  Task 9 · implement · attempt 7    sonnet · high 14 min  $0.88  "Task 9 is complete…"  │
│ …                                                                                              │
│ ─ plan ────────────────────────────────────────────────────────────────────────────────────────│
│ ✓ exit 0  plan · refine round 2             opus · high   9 min   $1.40  "Refine round 2: …"    │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
```

- **Every session of the run**, grouped by phase, newest first. The data comes from `agent_started`/`agent_session`/`agent_exited`, `state.exited` and `session_costs`. Each row carries the human label, role, model and effort (with today's reason tooltip), duration, cost, context %, exit code, and the first line of the step's result `summary`.
- **The transcript viewer** opens a session, at `#/run/<id>/agents/<session>`. The server parses the JSONL into turns:
  - the assistant's text;
  - each tool call as one line (`Bash: pytest tests/integration/…`, `Edit: src/…/kpi.py`) that expands to its input and a size-capped result;
  - thinking, folded.

  `tool_progress`, `system` and `rate_limit_event` records collapse into one "waited 40 s" line. A live session streams new turns (§4.10). The viewer keeps the dashboard's existing caps and `_tail` safety rules, applied per page instead of to one 16 KB tail.
- **Attach** keeps today's `agentflow attach <id>` copy box, moved onto each live session's row.
- **Parallel-ready.** The card on the Board shows "2 agents" with the two labels, instead of `live[-1]` only. That is needed as soon as the throughput proposal's parallel build runs more than one session per run.

### 4.6 Inbox: decisions, not keys

"Needs human" moves from the bottom of the Board to its own tab, with a count badge in the header that every view can see.

```
┌ Inbox · 3 waiting ────────────────────────────────────────────────── Group: [by run | by kind] ┐
│ ┌ Approve the spec ─────────────────────────── issue-81 · define · 12 min ago ───────────────┐ │
│ │ SPEC-issue-81.md · 191 lines · 3 doubt rounds · 1 medium finding open (rides along)        │ │
│ │ Approving starts plan (it moves on by itself). Rejecting runs define again with a note.    │ │
│ │ [Read the spec]   [Approve]   [Request changes…]                                            │ │
│ └─────────────────────────────────────────────────────────────────────────────────────────────┘ │
│ ┌ Task 9 is blocked: floor_guard ─────── issue-81 · build · 9 h 11 min · blocks 1 task ─────────┐ │
│ │ floor_guard fails again on the rename-only test_kpi.py finding because the human's approval │ │
│ │ commit 5ce7be0 was reset away …                                       [Full text ▾]          │ │
│ │ Facts the resolvers found (2) ▾      Evidence: test_kpi.py diff ▾                            │ │
│ │ Note to the agent (what changed or what to do): [_______________________________________]   │ │
│ │ [Approve and resume]   [Send task 9 back…]   [Open run]                                      │ │
│ └─────────────────────────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

- **A title in words** for each kind: approve the spec, approve the plan, answer N questions, a blocked task, a guard stop, CI failed, budget reached, an environment fault. Then the run's short name, phase, age and blast radius.
- **The inbox file's text, readable.** Its Markdown goes through the same renderer as 4.3. The "`agentflow approve …`" CLI lines are hidden behind **CLI equivalent ▾**, because the buttons do the same thing.
- **Forms in place.** The blocked note, required for `blocked_repeat` as today, is a textarea on the card instead of `prompt()`. Questions render as radio options with the recommended one pre-selected and its rationale shown, plus a free-text field. That replaces today's text box where you type the option key.
- **Context in place.** Phase approvals embed the artifact summary and a **Read the spec/plan** button (4.3). Stops show the run facts and the evidence the resolver attached.
- **Batch view.** "Group by kind" lists every waiting spec approval across runs together. That's the UI for the throughput proposal's batched approvals (§4.1.5), and it works today without that slice.
- **Keyboard.** `j`/`k` move between items, `o` opens, `a` approves after a confirmation step, `r` rejects. Every shortcut is also a visible button.

### 4.7 Board ergonomics

- **Cards** show a phase progress strip (the stage rail in miniature), the task count for `build` (`9/24`), a "needs you" marker in the waiting colour with its age, and the live agent count. Card text uses the human labels of 4.2.
- **Filters:** lane, status, **needs me**, and a text search over module, issue number and branch. They are kept in the URL hash.
- **Done** shows the newest 10 cards by default, with **Show all (N)**. The data stays as `DEFAULT_DONE_DAYS = 0` keeps it.
- **Peek:** Space on a focused card, or a click on the card's status, opens the existing drawer, trimmed to facts, the waiting decision and **Open run**. A click on the card opens the Run page.

### 4.8 The global Agents tab

The global tab keeps its role (all live agents across runs). Its rows are grouped by run under a run header, labelled as in 4.5, and link to the session's transcript, not just to the run. The "Exited" window stays at 10 minutes there, because the per-run Agents tab now holds the history. An idle agent (`idle_warn_min`) rises to the top with the idle time and its last tool.

### 4.9 Activity tab

This replaces the 300-event `key=value` timeline.

- **A story, not a log.** Each event type gets one sentence template, for example:
  - `step_finished` → "Task 9 · implement finished: *summary*";
  - `hitl_raised` → "Stopped for you: *summary*";
  - `phase_approved` → "You approved define".
- **Filters:** Decisions (human and runner), Agents, Gates, Commits, Stops, and All. `agent_turn` (73 % of the log) is hidden by default and summarised as "Agent active 14 min, 37 tool calls" between its neighbours.
- **The full history**, paginated by `seq` (`?before=<seq>&limit=200`), so issue-81's 5,232 events are all reachable.
- **Raw:** each line expands to its JSON for the person debugging the runner.

### 4.10 Live updates that keep your place

**On the page:**
- SSE messages are bucketed by `run_id`, and the client keeps per-run dirty flags. The Board refetches at most every 2 s, and only when a non-`agent_turn` event arrived (an `agent_turn` patches the card's activity line in place).
- An open Run page refetches only its own run, only the active tab's data, and patches by key: rows are keyed by step key, session id or task number, so nothing the user opened is rebuilt.
- Open `<details>`, scroll position, the selected revision and text being typed survive every update. If the decision being typed against disappears (someone approved from the CLI), the form shows "Approved from the CLI at 14:02 by …", and the text stays copyable.

**On the server:**
- `run_detail` no longer embeds the raw `state` (267 KB for issue-81). It returns the fields the page uses, plus `actions`. `?raw=1` keeps the old payload for debugging.
- `board()` caches each run's digest and metrics by `(events.jsonl size, mtime)`, as `_METRICS_CACHE` already does for ended runs, so an unchanged run costs a `stat()`, not a re-read.
- An SSE query option `?quiet=1` turns `agent_turn` into an `agent_activity` message at most every 2 s per session: `{session, last_tool, ts, context_pct}`.

### 4.11 API changes

All routes are new and read-only (GET), behind the same token, Host and Origin checks, with run ids validated by `RUN_ID` and every file read through the run's own paths or git objects, size-capped.

| Route | Returns | Source |
|---|---|---|
| `GET /api/runs/<id>` (changed) | today's fields minus raw `state`, plus `actions: [...]`, `label`, `stage_rail` | `display_status`, `run_control` preconditions |
| `GET /api/runs/<id>/artifacts` | `[{path, phase, kind, revisions: [{sha, step, round, ts}]}]`, where `kind` is spec, plan or file | `phases.<p>.start_sha`, `changed_since`, `step_finished.sha` |
| `GET /api/runs/<id>/artifact?path=&sha=` | `{text, sha, lines, truncated}`. `path` must be in the run's artifact list; `sha` must be one of its revisions or the branch head | `git show <sha>:<path>` in the repo |
| `GET /api/runs/<id>/artifact/diff?path=&from=&to=` | unified diff, capped | `git diff` |
| `GET /api/runs/<id>/findings` | findings by step and round, `{severity, file, line, text, open}` | `step_findings`, `review_result`, `state.reviews` |
| `GET /api/runs/<id>/plan` | `{path, sha, tasks: [{n, title, depends_on, files, status, attempts, review, commit, cost, blocks, blocked_by}]}` | `sources.py` + `state.steps` + `task_committed` |
| `GET /api/runs/<id>/sessions` | every session: label, role, model, effort, times, cost, exit, summary | `agent_*` events, `state.exited`, `session_costs` |
| `GET /api/runs/<id>/sessions/<session>/transcript?after=<n>` | parsed turns, paginated | `transcripts/*.jsonl` |
| `GET /api/runs/<id>/events?before=<seq>&kinds=` | events with a `text` sentence, paginated | `events.jsonl` |
| `GET /api/runs/<id>/files?name=` | `issue.md`, `pr.md`, `handoffs/*.md`, `refine/*.md`, `progress.md`, rendered by the page | the run directory, an allow-list of names |
| `GET /api/runs/<id>/changes` (added with U2) | `{base, head, files: [{path, status, added, deleted}]}`; with `?path=`, that file's diff, capped | `git diff <merge-base>...<branch>` |
| `GET /api/inbox` | items with `title`, `context`, `blast_radius`, `actions` | `inbox_items` + the above |
| `POST /api/runs/<id>/approve` (body extended) | accepts an optional `viewed: [{path, sha}]` and records it on the approval event | `hitl.approve` |

The one write-side change is `viewed`. It is optional, informational, never required by any obligation, and the CLI never sends it.

---

## 5. What stays exactly as it is

- **The security model** in `dashboard.py`'s docstring: `127.0.0.1` only, the token in the fragment, Host and Origin checks, a strict CSP with a per-response nonce, `no-store`, JSON escaping, and `textContent` for every string from a run. The Markdown renderer and the diff view build DOM nodes. No route reads a path the client names outside the run's own artifacts, files and git objects.
- **Engine dependencies** stay stdlib + `pyyaml>=6`. The page stays plain JavaScript with no build step. If `dashboard.html` gets too large (§9, Open Question 1), it splits into a few static files served with the same nonce, still from package data.
- **Project-agnostic.** Artifact kinds come from the phase that wrote them, not from path names. `test_portability.py`'s denylist covers the page as it does today.
- **CLI parity.** Every action is the same `run_control`/`hitl` call the CLI makes, with `by="dashboard"`. The page never decides anything the runner doesn't.
- **The Board, Workflow, Metrics, Lessons and Settings tabs** keep their data and routes. Settings keeps its §3.9 rule: a new config key ships with its Form field in the same PR. This proposal adds four `dashboard.*` keys (§8).

---

## 6. Measuring it

- **`viewed` on approvals.** `phase_approved` gains an optional `viewed: [{path, sha}]`, and `signals.py` gets an `informed_approval` key: approvals of `define` and `plan` whose `viewed` includes the newest revision of every artifact the phase produced. It's a KPI on the scorecard, which ties UI quality to approval quality.
- **Time to decision** by inbox kind (`hitl_raised` → answer or approval), already derivable. It is reported per kind, before and after slice U4.
- **Touches per decision:** dashboard actions per inbox item, including refused ones. The server log already records each refusal with its reason, and a counter makes it a metric.
- **Refresh cost:** requests per minute and bytes served while one agent runs, logged by the dashboard at `--verbose`. Target: from up to 4 full refreshes a second to ≤ 0.5 scoped ones.
- **A check with the user:** after U1 and U4, the person running agentflow day to day uses it for one week and notes every time they had to leave the dashboard to understand a run. The list is the backlog for the next slice.

---

## 7. Rollout

Each slice is one PR on agentflow's own feature lane, with a plan in `docs/plans/`, ordered by **attention saved per unit of risk**. No slice changes runner behaviour.

| Slice | Content | Risk | What the user gets |
|---|---|---|---|
| **U0 Foundations** | Hash routes (`#/run/<id>/<tab>`); a Run page shell with header, stage rail, actions and Overview; the drawer becomes the peek; patch-by-key rendering with UI state preserved; SSE bucketing and the 2 s board throttle; slim `run_detail` with `actions`; per-run digest cache | low | The page stops collapsing; every run has a URL; no wrong-state buttons |
| **U1 Spec and plan** | Artifacts, artifact, diff and findings routes; the Markdown renderer; the Spec and Plan (Document) tabs with revisions, diff and gutter findings; Approve sends `viewed`; Request changes composes a line-anchored reject note | low–medium (new file reads, guarded by the artifact allow-list) | **Read and judge the spec and plan in the dashboard** |
| **U2 Process** | The `plan` route; the Plan (Tasks) board with status, attempts, review, cost and blast radius; group by wave; the Files tab with `issue.md`, per-phase handoffs, `pr.md`, refine notes and the branch diff | low | Follow the build task by task; see what a stop holds up |
| **U3 Agents** | The `sessions` and `transcript` routes; the per-run Agents tab; the transcript viewer, live-streamed; multi-agent cards; the global Agents tab grouped by run | medium (transcript parsing over large files; paginate and cap) | **Every agent of every run, and what it did** |
| **U4 Inbox** | The `inbox` route; the Inbox tab and header badge; titled decision cards with forms in place, context, grouping and keyboard; `informed_approval` in signals and the scorecard | medium (the main control surface changes; keep the old list behind `dashboard.inbox_legacy: true` for one release) | Clear a stop in 1–2 touches with its context in front of you |
| **U5 Board and Activity** | Card progress strip, filters and search, Done paging; the Activity tab with templates, filters and pagination | low | A calmer Board; the run's full history, readable |

U0 comes first because every later view needs routes and stable rendering. U1 is the user's sharpest pain and has no runner coupling, so it ships second. U4 depends on U1, because its cards link into the artifact viewer.

**Tests** (`agentflow/tests/test_dashboard.py`, 97 tests today):
- every new route gets the existing refusal matrix: no token, wrong Host, bad run id, a symlinked run directory, an oversized response;
- the artifact routes get path traversal, a `path` outside the artifact list, a `sha` that isn't one of the revisions, and a missing worktree that falls back to git objects;
- the Markdown renderer gets a fixture of hostile input (`<script>`, `javascript:` links, HTML entities) that must come out as text.

---

## 8. Config keys

Each ships with a Settings Form field, `validate` checks, a `test_config_edit` case and a playbook §14 line (the four companions).

| Key | Default | Meaning |
|---|---|---|
| `dashboard.board_refresh_s` | 2 | the most often the Board refetches while events arrive |
| `dashboard.done_shown` | 10 | Done cards shown before **Show all** |
| `dashboard.artifact_max_kb` | 512 | the largest artifact, transcript page or diff the page loads; above it, the page shows the head and a "copy path" note |
| `dashboard.inbox_legacy` | false | U4's fallback to the old Needs-human list, for one release |

None of them is protected.

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| `dashboard.html` (1,555 lines) grows hard to maintain | Group the code by view with a small shared `el()`/router core. Open Question 1 allows splitting into static modules with no build step |
| A new file-read route leaks files outside the run | Reads only through the run's artifact list, an allow-list of run-file names and `git show` of the run's own revisions. No client-supplied filesystem path. The existing `_tail` rules (no symlinks, inside the root) and size caps apply. Traversal tests in U1 |
| The Markdown renderer becomes an XSS vector | DOM construction with `textContent` only; a link allow-list (`http(s):`, repo-relative); a hostile-input fixture test. The CSP already blocks inline script as a second line |
| Transcript parsing is slow on large sessions | Paginate by line offset and cap the bytes per page. Cache parsed pages by `(file, size, mtime)` |
| The plan's format varies by pack, so task parsing misses tasks | Reuse `sources.py`, the parser the runner itself uses, so the Plan tab shows exactly the tasks the runner walks. If it can't parse the plan, it falls back to the Document mode |
| Line comments make reject notes long and over-specific | The composed note is shown for editing before it is sent, and capped at `TEXT_MAX` as today |
| Users miss the old drawer | The drawer stays as the peek, and every old section has a place on the Run page (Overview, Files, Agents, Activity, Gates) |

## 10. Open Questions

1. Should the page stay one `dashboard.html`, or split into `dashboard/*.js` static files served with the nonce and no token (they hold no data)? Proposed: split in U0, before the file doubles.
2. Should the Spec tab allow a **direct edit** of the spec before approval, written by the runner into the worktree as a human commit with a trailer? It would save a reject → refine round for small wording fixes, but it adds the first write path from the page into a repository. Proposed: not in this proposal; revisit after U1's data on how often the human requests changes.
3. Should `informed_approval` ever become a policy obligation (say, approval refused unless the artifact was viewed)? Proposed: **no**. It's a signal, not a gate. The CLI approves without a browser.
4. Should the Inbox also show runs that are **held** (usage limit) or **stalled** (no event for `step_timeout_min`)? They need no decision, but they do need attention. Proposed: yes, as a separate "Watch" group with no buttons.
5. One dashboard per repo, or one for every registered repo (`agentflow-harness-proposal.md` §150)? The Inbox is where cross-repo serving pays off. Proposed: keep it per repo until the Inbox is in use, then add a repo switcher with a combined Inbox.

## Verification

- **U0:**
  - With a fake run appending 10 `agent_turn` events a second, an expanded `<details>` on the Run page stays open and the scroll position doesn't move.
  - `/api/board` is called at most once per `board_refresh_s`.
  - `#/run/<id>/plan` survives a reload.
  - The `actions` list for a `done` run doesn't contain `pause`, and the page shows no Pause button.
- **U1:**
  - For a fixture run whose `define` wrote a spec in 3 revisions, `/artifacts` lists 3 revisions, and `/artifact/diff` between r1 and r2 returns the change.
  - A `step_findings` finding on line 34 renders a gutter dot on line 34.
  - `?path=../../state.json` and a path outside the list are 404.
  - With the worktree deleted, the artifact still reads from git.
  - Approve sends `viewed`, and `phase_approved` records it.
  - The hostile Markdown fixture renders as text.
- **U2:**
  - On a 5-task fixture plan where task 2 is blocked and tasks 3 and 4 depend on it, the Plan tab marks task 2 blocked with "blocks: 3, 4" and shows task 5 as independent.
  - The Files tab shows `handoffs/build.md` in full when `handoff.md` exceeds 16 KB.
  - After a merge of `origin/<fork_from>` into the branch, `/changes` lists only the run's own files.
- **U3:**
  - A fixture run with 3 sessions on one step (attempt 1, attempt 2, resolver 1) lists all 3 with distinct labels.
  - The transcript route returns parsed turns, and `tool_progress` lines are collapsed.
  - Pagination returns every line of a 5 MB transcript in capped pages.
- **U4:**
  - A `blocked_repeat` item can't be approved with an empty note: the form refuses before the request.
  - A question renders its options with the recommended one pre-selected.
  - Approving from the CLI while the card is open shows "approved from the CLI" and keeps the typed text.
  - `informed_approval` appears in `agentflow scorecard`.
- **U5:**
  - The Activity tab reaches the first event of a 5,000-event fixture.
  - Filtering to Decisions shows only `hitl_*`, `phase_*`, `checkpoint_cleared` and runner decisions.
- **All slices:**
  - `test_dashboard.py` and `test_portability.py` are green.
  - `validate` reports 0 warnings for the new keys.
  - The CSP header is unchanged.
