# Slice LR: lessons and retro (the self-improvement loop)

## Context

agentflow runs for hours and records every run in `runs/<id>/events.jsonl`, but nothing it learns outlives the run. `facts` (8) and `code_map` (40) only feed the same run's prompts (`lanes.py:134-153`), and `metrics.aggregate` reports only time, cost and rework. So the same gate failures, guard trips, human rejections and review findings come back run after run.

The design for this already exists in `docs/agentflow-harness-proposal.md` §3.10 tier 5 ("learning across runs is gated, never self-written") and is roadmap slice **3b**. It was ordered after the conductor (slice 3), which isn't built. **Decision (user):** ship 3b now as its own slice. Lessons go into ordinary step prompts (no conductor). The retro may change **only the lessons file**. Lessons reach the repo **only as a draft PR that a human merges**. A retro runs **on demand (`agentflow retro`) and whenever `memory.retro_every_days` has passed**.

The loop:

```
runs/*/events.jsonl ──(runner, deterministic)──▶ retro digest (data, with <run>#<seq> pointers)
        ▲                                               │
        │                                     retro agent session (read-only)
        │                                               │ result.json: lessons.add / expire / renew
        │                                     runner checks the evidence → renders .agentflow/lessons.md
        │                                               │ commits on agentflow/retro-<date> → draft PR
        │                                         human merges (the runner never merges)
        └──── {lessons} slot in _preamble.md ◀── engine lessons.md + pack .agentflow/lessons.md (main checkout)
```

## What goes into lessons.md (and what doesn't)

`lessons.md` holds **rules an agent can act on**, not a log of metrics. Every agentflow signal goes into the **digest**, which is the retro's input. A signal becomes a lesson only when a change in agent behaviour would have avoided it. When the cause is a setting (a budget that is too low, the wrong model, a slow gate), the retro may not change config, so it writes a **config note** in the PR body. Nothing applies that note; a human decides.

| Signal (all go into the digest, with `<run>#<seq>`) | Source events | Can become a lesson? | Example output |
|---|---|---|---|
| Gate failures and fix loops | `gate_result` (+tail), `fix_attempt` | Yes, the most common kind | "Run `ruff format` before reporting a build task done (gate `lint` failed first pass in 6/9 runs)" |
| Guard trips and permission denials | `guard_tripped`, `forbidden_reverted`, `permission_denied` | Yes | "Never edit `tests/fixtures/explain/`; regenerate nothing there" |
| Human rejections, overrides, refine notes | `phase_rejected` (by human), `hitl_answered` (human ≠ recommended) | Yes, the highest-value kind | "Plans must name the test file for each task (plan rejected twice for this)" |
| Review blocking findings | `review_result`, `step_findings` | Yes, when a finding repeats | "Check every new config key ships its Settings field and §14 line" |
| CI failures | `ci_round` (+check names, tail) | Yes, when the failure is local-reproducible | "Run the full suite, not just touched files, before `finish` when `pack.py` changes" |
| Cost and budget overruns | `budget_soft_reached`, `budget_estimate_over_hard`, `budget_scaled`, `agent_exited.cost`, rework cost | Sometimes, when the cause is behaviour; otherwise a config note | Lesson: "Don't read whole design docs; grep `^## ` first". Config note: "`task_budget_usd` is hit in 40% of build tasks" |
| Turns, tokens, context, rollovers, compactions | `agent_turn` (tokens, context_pct), `rolled_over`, `compacted`, `context_window` | Sometimes | "Update `progress.md` after each sub-task" (from `progress_not_updated`); config note: "`max_rollovers` reached in 3 runs → split tasks" |
| Time (wall time split into agent/gates/human/ci/hold) | `metrics.run_metrics` buckets | Rarely, mostly config notes | Config note: "gate `engine_tests` is 70% of verify time" |
| Runner errors, timeouts, stuck steps | `runner_error`, `step_timeout` stops, `blocked_repeat` | Rarely; usually an engine bug | PR body section "Engine issues seen", for a human to file |

So the PR has three parts. **Lessons**: the only file change. **Config notes**: advice only. **Engine issues seen**: advice only. Each line in all three cites its evidence and its numbers (count, cost, time), so the human can see why it is there.

## Design decisions (and where they depart from §3.10)

- **Two layers, as proposed.** The engine `agentflow/lessons.md` holds model-behaviour lessons that hold in any project. It ships with releases and is edited by hand, so it must pass `test_portability`. The pack `.agentflow/lessons.md` holds this project's lessons and is the only file the retro writes. A lesson that looks generic is listed in the PR body as "candidate for the engine layer", for a human to promote.
- **The path is fixed, not a config key.** `.agentflow/lessons.md` already sits under the engine floor (`pack.ENGINE_FORBIDDEN = .agentflow/**`), and the engine only ever reads the main checkout's pack (`pack.pack_config_path`). So no agent in a run can edit it, and an edit made in a worktree is never read. This replaces the proposal's protected `memory.lessons_path`. The proposal's `stats.json` is deferred to slice 3, which will have a consumer for it. For now the retro digest carries the same numbers.
- **Lesson format.** Markdown that a human can edit and that the engine parses strictly, in a new `agentflow/lessons.py`:
  ```
  ## L-0007 Run the portability test before finishing a build task
  - applies: build, verify            # phase names; omitted means every phase
  - source: 20261105-0912-feature-pack#418, 20261106-1400-bug-gates#77
  - review_by: 2027-02-01
  - target: gate_first_pass:portability     # a metric key from metrics.py's fixed vocabulary
  - baseline: 3/9 runs (33%) to 2026-11-08 # written by the RUNNER from the evidence window, never by the agent
  The portability gate fails on project strings in engine files; grep DENYLIST first.
  ```
  The body is at most 300 characters. A malformed entry is a static `validate` warning. A lesson past `review_by` is **left out of prompts**. The digest lists it, so the retro proposes to renew or expire it. `validate` gives no date-based warnings, so a passed date can't turn the "0 warnings" check red.
- **Only runner-checked evidence goes in.** Every `add` needs at least one `source` that resolves to a real event in a real finished run. A source that doesn't resolve drops the lesson, with a note in the PR body. This blocks invented or poisoned lessons. The digest quotes agent, CI and log text as data, and `prompts/retro.md` says so. At most 5 adds per retro (a constant).
- **A retro is a reserved engine pipeline `retro`, not a lane.** `state.check_lane` accepts it, it never appears in `lanes:`, so the `explain` golden files and lane lists don't change. It is a real `Run`, so it gets events, budget, the inbox, the dashboard and the cost metrics for free. Retro runs are left out of every later digest, so a retro never learns from retros.
- **The runner commits; the agent doesn't.** The retro session returns proposals in its result JSON (a new optional `lessons` field). The runner renders the file in a worktree on `agentflow/retro-YYYYMMDD` (`gitops.add_worktree`) and commits through a new `gitops.commit_lessons()`. Unlike `gitops.commit`, it allows `.agentflow/lessons.md` and **nothing else**: any other staged path refuses the commit. The runner then pushes and opens a draft PR (`gitops.create_draft_pr`). It never merges.
- **Lesson hit rate.** The result schema gets an optional `lessons_cited: [ids]`, and the runner emits `lesson_cited`. For each lesson the digest shows its citations and the KPIs of the runs that cited it, so the retro can propose to expire a lesson that is never cited or that is followed by regressions.

## How you know a lesson improved anything

Three things are recorded, all by the runner from events, so no model text can fake them:

1. **What the lesson targets.** Each retro-added lesson names one `target` metric from a fixed vocabulary that `metrics.py` computes. Examples: `gate_first_pass:<gate>`, `fix_attempts:<phase>`, `guard_trips:<kind>`, `permission_denied:<tool>`, `stop:<kind>`, `human_rejections:<phase>`, `review_blocking:<phase>`, `ci_rounds`, `cost_per_task:<phase>`, `turns_per_step:<phase>`, `rollovers`, `progress_not_updated`, `rework_pct`. When the runner renders the lesson, it computes the `baseline` from the runs before the merge. A hand-written lesson may leave out `target`; it then shows as "not measured".
2. **Which runs actually got it.** Each fresh session emits `lessons_injected` with the lesson ids and a hash of the lessons file. This separates runs that had the lesson from runs that didn't. "Merged" alone doesn't settle that, because a run started before the merge never saw it.
3. **Before and after.** The runner compares the target metric in the baseline runs against the runs that had the lesson injected. Each lesson gets one verdict:
   - **improved**: the metric moved the right way
   - **no change**
   - **worse**
   - **too few runs**: fewer than 3 after-runs
   Each verdict shows its numbers ("lint first pass 33% → 86%, 7 runs since 2026-11-10") and how many times the lesson was cited. The numbers show correlation, not proof, and the report says so.

Where you see it:
- **`agentflow lessons`** (`--json`): a table with id, title, target, baseline → now, verdict, citations and review_by.
- **The dashboard Lessons tab**: the same table, plus a link to each lesson's evidence runs.
- **The next retro PR** opens with **"Effect of previous lessons"**, which carries these verdicts. The retro proposes to expire a lesson that came out **worse** or was never cited by its `review_by` date, and to renew one that came out **improved**. A human merges either way.

## Tasks

1. **Richer failure signals** (`lanes.py`, `hitl.py`, `ci_lane.py`, `run_control.py`). Add a capped `tail` (≤ `context.gate_output_lines`) to `gate_result`, a capped `extra` to `hitl_raised`, failed check names plus a tail to `ci_round`, and an optional `reason` to a human's `step_retry` (`flow.py retry --reason`). Today these details live only in the inbox and log files, which a later stop overwrites (`inbox/<key>.md`). Reducers ignore the new fields. Tests: `test_state.py` and `test_agent_step.py` cases for the new fields.
2. **Failure and efficiency metrics** (`metrics.py`). Extend `run_metrics`/`aggregate` with:
   - **Failures:** gate failures by gate name and first-pass rate, stop kinds, guard trips by kind, permission denials by tool, review findings by severity, runner vs human answers, human rejections, CI rounds, runner errors.
   - **Efficiency:** turns per step, tokens per step (input/output, from `agent_turn`), peak context %, rollovers and compactions, `progress_not_updated`, and the budget events (soft reached, estimate over hard, scaled). These sit beside the existing cost, rework and time buckets. `agentflow metrics` and the dashboard Metrics tab show them (`views.metrics_view`). Tests: `test_metrics.py`.
3. **`lessons.py`.** Parse, validate and render both layers. Add `select(phase, today, budget_tokens)` with oldest-first truncation, which emits `memory_truncated` and drops expired lessons. Seed `agentflow/lessons.md` (engine) by hand from the existing comments in `defaults/config.yaml` (generic rules only), and create `.agentflow/lessons.md` with a header for this repository. Add a `pack.lessons_paths()` helper that resolves the main checkout's pack file plus the engine file. Tests: new `test_lessons.py`, plus `test_portability.py`, which covers the engine file automatically.
4. **Injection.** Add a `{lessons}` slot to `prompts/_preamble.md` next to `{run_facts}`, add it to `lanes.EMPTY_SLOT` and `config.PROMPT_PLACEHOLDERS`, and fill it in `lanes._values` from `lessons.select`. It goes into fresh sessions only, the same as run facts. Add `lessons_cited` to `_result.md` and to the parser in `hitl.py`, and add a `lesson_cited` event. Emit `lessons_injected` (ids plus a file hash) for each fresh session. Tests: any test that compares an exact preamble must stay green. If one compares whole prompts, the fixture packs set `memory.lessons_max_tokens: 0`. Check that the `explain` goldens stay byte-identical (explain doesn't print prompts, so they should).
5. **Retro digest** (`retro.py: build_digest(repo, since)`). It reads the finished, non-retro runs since the last retro, from events only. It writes per-run lines with `<run>#<seq>` pointers: gate tails, fix attempts, guard trips, stop extras, human rejection notes, answers where a human overrode the runner, blocking findings and CI failures. It adds the task-2 aggregates and a lessons table (id, cited count, review_by, expired), capped at `memory.digest_tokens`. Tests: a fixture of event logs gives a stable digest.
6. **Retro session and PR** (`retro.py`, `prompts/retro.md`, `skills/agentflow-retro/SKILL.md`, a top-level `retro_step` in `defaults/config.yaml` like `resolve_step`). The runner sets up the run and the worktree, puts the digest in the workspace (`workspace.prepare`), and runs one session through `Executor.run_agent_step` with a plain `Phase`, the way `autonomy.Resolver._run_session` does. It reads the `lessons` result, checks the evidence, renders the file, runs `commit_lessons`, pushes, and opens a draft PR. The body has three sections: Lessons (each with its evidence), Config notes and Engine issues seen. The result fields `config_notes` and `engine_issues` are text only, and the runner checks their sources the same way. With no changes it opens no PR and finishes with "nothing to learn". Tests (fake agent, fake `gh`): the happy path opens a PR; an unresolved source is dropped; a staged path other than lessons is refused; a retro run is left out of the next digest.
7. **Lesson effect report** (`lessons.py: effects(repo)`, `flow.py lessons`, `views.py`/`dashboard.html` Lessons tab). It parses `target`/`baseline`, validates `target` against the metrics vocabulary (static `validate` warning), and splits runs into baseline and injected using `lessons_injected`. It computes the verdict, with 3 runs as the minimum after-sample (a constant). The retro digest and the "Effect of previous lessons" PR section reuse it. Tests: fixture event logs give improved, worse and too-few-runs verdicts. A run started before the merge counts as baseline.
8. **Triggers.** Add `agentflow retro [--since DATE] [--dry-run]` in `flow.py`. "Retro due" is when `memory.retro_every_days` (0 means manual only) has passed since the last retro run **and** at least one finished run is newer. `next`, `status` and the dashboard show it, and `queue` runs a due retro between modules. There is no daemon. Tests: due/not-due cases.
9. **Config keys**, each with all four obligations (a Form field or a `_PROTECTED_KEYS` entry in `config_edit.py`, a `validate` check, a `test_config_edit.py` case, and a line in playbook §14):
   - `memory.lessons_max_tokens`: Form, default 1500, validate 0…4000.
   - `memory.digest_tokens`: Form, default 15000, capped.
   - `memory.retro_every_days`: Form, default 7.
   - `retro_step`: protected, the same as `resolve_step` (`config_edit.py:49`).
   Add `memory` to `defaults/config.yaml` and a `MemoryPolicy` dataclass in `config.py` (like `ContextPolicy`). It is not a `WORKFLOW_KEYS` entry.
10. **Docs and release notes.** Update proposal §3.10 and §5: 3b is done standalone, the path is fixed, and `stats.json` is deferred. Write playbook sections on running a retro and on reviewing a lessons PR. Add a `CHANGELOG.md` `[Unreleased]` "Added" line. Write the plan record `docs/plans/2026-11-08-agentflow-harness-slice-lr.md`. Update the engine-architecture paragraphs in CLAUDE.md.

**Security overlay applies.** The slice touches `pack.py` and `gitops` commit rules, and puts run-derived text into prompts. Plan and review it with `security-and-hardening` and the `security-auditor` agent. Focus on three things: the `commit_lessons` allowlist, evidence checks that resist an invented `run#seq`, and the rule that digest text is data.

## Critical files

- New: `agentflow/lessons.py`, `agentflow/retro.py`, `agentflow/lessons.md`, `agentflow/prompts/retro.md`, `agentflow/skills/agentflow-retro/SKILL.md`, `.agentflow/lessons.md`, `agentflow/tests/test_lessons.py`, `agentflow/tests/test_retro.py`
- Change: `lanes.py` (`_values`, `EMPTY_SLOT`, `gate_result` tail), `prompts/_preamble.md`, `prompts/_result.md`, `hitl.py`, `ci_lane.py`, `run_control.py`, `metrics.py`, `views.py`, `state.py` (`check_lane`, reducers for `lesson_cited`/`memory_truncated`), `gitops.py` (`commit_lessons`), `pack.py`, `config.py`, `defaults/config.yaml`, `config_edit.py`, `flow.py`, `module_queue.py`, `dashboard.py`/`dashboard.html`
- Reuse: `autonomy.Resolver._run_session` pattern, `Executor.run_agent_step`, `Run.create`, `gitops.add_worktree`/`push`/`create_draft_pr`, `metrics.aggregate`, `views.metrics_view`, `workspace.prepare`, `safeio`

## Verification

- Per task: run the touched test files with `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/<file> --import-mode=prepend -q`. Then run the full suite before finishing, and `test_portability.py`.
- `bin/agentflow validate` prints "0 warnings". `git diff --exit-code agentflow/tests/fixtures/explain/` shows the goldens unchanged.
- End to end in a temp repo (the `test_plug_and_play.py` style): two fake-agent runs that fail the same gate, then `agentflow retro`. Check that the digest cites both runs, that the fake retro result produces a draft PR (fake `gh`) whose only changed file is `.agentflow/lessons.md`, and that after a simulated merge the next run's first prompt contains the lesson under `{lessons}`. Then set `review_by` in the past and check that the lesson leaves the prompt. Finally, script three more fake runs that pass the gate on the first try, and check that `agentflow lessons` shows the lesson as **improved** (33% → 100%). Check that a second retro's PR opens with "Effect of previous lessons" carrying that verdict.
- Manually: run `bin/agentflow retro --dry-run` on this repository's real state dir, read the digest, and run `agentflow dashboard` to see "retro due" and the new Metrics rows.
