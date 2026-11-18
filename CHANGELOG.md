# Changelog

All notable changes to agentflow harness are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

The version is `pack.ENGINE_VERSION`; `pyproject.toml` must carry the same number (`test_packaging` checks it).

## [Unreleased]

### Added

- `agentflow doctor` has a `claude user settings` check (when `agents.claude.user_settings` is off) that names what your own Claude settings hold that run sessions no longer load (enabled plugins and hook events), and warns about `env`, `apiKeyHelper` and `permissions` there; it reads one file and prints key names and counts only.
- The Metrics tab shows what share of cost went on cache writes, and a "Where the tokens go" table by step kind (sessions, cost by token class, turns p50 and p90, prefix write, tool-output size).
- Six cost signals join the vocabulary a lesson can target (`cache_write_share`, `prefix_write_tokens`, `session_turns_p90:<kind>`, `tool_output_kchars:<kind>`, `compactions`, `usage_holds`), so `lessons`, `scorecard` and the retro digest see them; the scorecard's baseline for them starts at the next `agentflow scorecard --freeze`.
- Run metrics split a run's cost by token class (`usd_split`, `tokens`) and by step kind (`by_step_kind`: sessions, cost, turns, prefix writes and tool-output size per kind such as `build.implement`), and `aggregate` adds `usd_split` and `step_kinds` with the median and p90 over every run's sessions. A run from before `usage` shows empty splits.
- The run state keeps the latest `usage_window` per window (`usage_window`), and a run notes once per setting, in a `cli_setting_ignored` event and `cli_settings_ignored`, when the Claude CLI did not honour `cache_ttl` (the writes landed in the other lifetime) or `lean` (auto-memory was still on). The run goes on.
- `agent_exited` carries the invocation's `usage`: its tokens and USD split by input, output, cache read and 5-minute and 1-hour cache writes (summed over every `result` the invocation sends, never the session's resumed total, and estimated from the messages when no result reported any), its turns, terminal reason, prefix write, tool-output size and whether auto-memory was on. A new `usage_window` event records the five-hour and seven-day utilization when it moves by 5 points or its status changes.
- `agents.claude.cache_ttl` (`5m`, `1h` or `cli`), `lean` (on by default) and `user_settings` (off by default; the user file may set it) are new keys for Claude run sessions, with `validate` checks, Settings form fields and a note for Claude Code 2.1.327 or later.
- With `route.mode: observe` or `on`, the runner raises a run's route profile mid-flight when the plan turns out bigger or riskier than the triage said (more than 3 open tasks: `M`; 12 or more, a task the plan sizer flagged `risk`, or the security overlay: `L`), before the phase starts and again once build's plan sizer answers, and never lowers it. Under an applied profile, per-task reviews run at `medium` unless the plan sizer flagged the task `risk` or the task's own files touch the security overlay's paths. The new `route_upgrades` signal counts the upgrades.
- With `route.mode: on`, the run's route profile applies: the steps in its `skip` are skipped, `doubt_rounds` caps each phase's doubt rounds (never above `autonomy.finding_rounds`, never below 1), and `per_task_review: false` drops the review of a plan task the plan sizer did not flag `risk`, outside the security overlay. `agentflow explain --profile <P>` shows the steps a profile skips.
- With `route.mode: observe` or `on`, a short read-only triage session sizes a run before its first phase and the runner records the route profile its rule picks (`route_profile`); the session may only propose a larger profile, and a triage that edits the repository stops the run as `sizer_edit`, checked before the first phase even if the pack later turns `route` off. `validate` reserves the step id `route`.
- `route`: profiles `XS`, `S`, `M` and `L` that skip steps, cap doubt rounds and drop per-task review, off by default (`route.mode`: `off`, `observe` or `on`). The engine ships the grid, `validate` keeps every obligation and the `finish` and `ci` steps out of a profile's reach, and the Settings form edits `route.mode`.
- The Board filters by lane, status, "needs me" and a search over module, issue and branch, kept in the URL, and Done shows the newest `dashboard.done_shown` cards first with **Show all**.
- Board cards show a phase progress strip, a "needs you" marker with how long it has waited, the live agent count and the step in words.
- `dashboard.done_shown` (10 by default, 1–1000): how many Done cards the Board shows before **Show all**.
- The Inbox groups decisions by run or by kind, takes `j`/`k`/`o`/`a`/`r` from the keyboard (approve takes two presses), and keeps a card you were typing on when the decision is made elsewhere, saying who made it.
- `informed_approval:<phase>` in signals, `agentflow scorecard` and the Metrics tab: the share of a person's phase approvals that viewed every newest revision the phase produced on the dashboard.
- `dashboard.inbox_legacy` (false by default): also show the old Needs-human list at the bottom of the Board (the Inbox tab stays), for one release.
- The Files tab shows the run's own documents in full (`issue.md`, each phase's handoff, refine notes, `pr.md`, `progress.md`), and the branch's changes since it left the base with a diff per file: the pull request's diff, readable before the pull request exists.
- The Plan tab's Tasks mode lists each plan task with its status, tries, review and cost, groups the tasks by status or by dependency wave, and shows what a blocked or parked task holds up and which tasks could go on without it.
- A run's Agents tab lists every session the run started (attempts, resolvers and sizers labelled in words) with model, effort, cost, exit and result, and opens each one's transcript as a readable conversation that follows a live session.

### Changed

- Claude run sessions now use the new `agents.claude` keys: the 5-minute prompt cache by default (neither an inherited TTL variable nor the pack's `defaults.env` can override it), a lean prefix (`--exclude-dynamic-system-prompt-sections`, `--strict-mcp-config`, no auto-memory) and `--setting-sources project,local`, and sizer sessions drop the skills listing (`--disable-slash-commands`). **Upgrade impact:** runs stop loading your own Claude plugins, hooks, MCP servers, `env` and `apiKeyHelper` from your user settings; `agentflow doctor` flags what yours hold, and `agents.claude.user_settings: true` in your user file keeps the old behaviour. `validate` refuses `--setting-sources` in `agents.<name>.base_args`, since the runner sets it. A CLI that refuses one of these flags stops the step as `blocked`, naming the flag and the fix, instead of being held as an outage.
- The engine's defaults now use `sonnet` for the `plan` and `debug` fallback models and the define, plan and build phases' planning models, and turn the watchdog on at `watchdog.no_progress_min: 15`. A pack that states its own values is unaffected.
- Decisions waiting on you move from the Board's Needs-human list to an Inbox tab with a count in the header. Each card has a title in words, the inbox text with its CLI lines folded away, the facts and artifacts it needs, what a stop holds up, and forms in place of the browser's prompt boxes. `dashboard.inbox_legacy: true` brings the old list back.
- A Board card lists every live agent of its run, not just the newest, and the Agents tab groups agents by run, puts idle ones first and opens each one's transcript.
- A run's Activity tab tells its whole log as sentences ("Task 9 · implement finished: …", "You approved define"), filtered to Decisions, Agents, Gates, Commits or Stops. It folds agent turns into "Agent active 14 min, 37 tool calls", pages back to the first event, and shows each line's raw JSON on demand. The run detail no longer sends its last 300 raw events.

### Removed

- The run detail's 16 KB transcript tail (`transcript`, `transcript_file`); the Agents tab's transcript viewer replaces it.

### Fixed

- A bare `route.mode: off` (which YAML reads as false) keeps the route off at run time instead of triaging every run; `validate` still names it. A route profile may no longer skip a group, repeat or step whose nested or `review` step is a `finish`, `ci` or `foreach` step or an obligation's `step:<id>` evidence, and the runner and `explain --profile` never skip a `finish`, `ci` or `foreach` step even in a pack that was not validated.
- A spec whose answered questions sit under a plain `Resolved:` line (or a `### Resolved` heading) no longer counts them as open: the default `spec_has_open_questions` stops at any of these, and the `record_decision` prompt files a recorded answer under a bold `**Resolved (...)**` heading.
- A repeat group whose round asked no questions while its `until` condition still fails stops for a human after that round instead of running the same round again, and its `max_rounds` stop names the condition (`not spec_has_open_questions`, not `inline condition`) and says why it still fails.

## [2.9] - 2026-11-17

### Added

- On the Spec and Plan tabs you can comment on lines. **Request changes** turns the comments into one reject note (`L34: …`) that you edit before it is sent, and **Approve** sits under the document it approves.
- A run's page in the dashboard has Spec and Plan tabs: each revision `define` and `plan` committed, rendered or raw, a diff with the previous one, and the review findings by round with a dot on their line.
- A dashboard approval of a phase records the spec and plan revisions the page showed (`viewed` on `phase_approved`). The CLI's approvals are unchanged.
- `dashboard.artifact_max_kb` (512 by default, 16–8192): the largest spec, plan revision or diff the dashboard loads.
- `gates.<name>.group` and `gates.<name>.resource`: gates of different groups run at the same time (each group in set order), and gates that share a resource never overlap within a run. Gates with no group run in order as before, and leaving early kills every group's processes.
- `gates.<name>.inputs`: a gate that passed in the run is not run again while none of the files its globs match has changed, even on another tree, and `conformance` accepts that pass as floor evidence on the final tree only when the gate's input hash there is equal. A step whose verify gates were reused still counts as verified work, so its commit leaves the earlier `step:` evidence standing. New `gate_cache_hit` event, `gate_result.input_hash`, and the `gate_cache_hit_rate` signal. Without `inputs`, nothing changes.
- `gates.<name>.affected` and `phases.<name>.steps[].gate_tier`: a step at `gate_tier: affected` runs each gate's `affected` command (`{run, paths}`) on the files it changed, each name shell-quoted (`validate` refuses a `{paths}` that is not its own unquoted word), and skips a gate none of them matches; above 200 paths or a 64 KiB command the gate runs in full. The engine defaults put `review/fix_findings` and `ship/finish.sync` at that tier; `ship/ci.fix` stays full, since the `ci` step pushes its fix with no audit after it. For the same reason, the merge `finish` repeats when GitHub calls the open pull request conflicting runs the `sync` gates in full. Such a result carries `gate_result.tier: "affected"` and is never reused, never floor evidence and never a full-suite run, so the full `verify` set still runs on the final tree. Without `affected`, nothing changes.
- When a step ran the affected tier, `finish` runs the full `verify` set once on the committed tree it is about to audit and push, reusing any pass that still holds there, so the audit has its evidence. A failure goes to a session of the `sync` step at the full tier (`ship/finish/final_fix`, with its fix loop), and the runner commits its fix before the audit, and a stopped `final_fix` session resumes on approval before anything else in `finish`. With no `sync` fix loop, or once that session is done, it is the audit's usual `gate_failed` stop. A run with no affected-tier result finishes as before.
- `ci.early_pr_after` (`""`, off, by default): the runner pushes the branch and opens the draft pull request as soon as the named phase is approved (`pr_opened` with `early: true`), so CI runs while review does. It is a draft only: `finish` still audits the final tree, then pushes it and updates the description (`pr_updated`) instead of opening another, and marking it ready still needs the audit and a green CI: the `ci` step watches it only after `finish` updated it, and reads its checks only while its head on GitHub is the sha the runner pushed last, the merge `finish` pushes again for a conflicting pull request included. A GitHub or push failure then is `early_pr_failed` and never stops the run; `finish` opens the pull request as usual. `validate` wants a phase before the one holding `finish`.

### Fixed

- The run page's Overview and the drawer draw a waiting decision again; the Spec and Plan tabs' approve bar had taken over its drawing function.
- The Backlog no longer shows a module `issue-<N>` whose GitHub issue is closed: such a module is done as `issue closed` once `gh` says so, unless a run of it is live (asked once, then recorded; `agentflow backlog undone` overrides it). Before, a closed issue's module stayed when its run directory was gone, since the branch checked for a merge lacked the issue title's slug.
- A plan names its spec for the "plan complete on <branch>" reason with a plain `Spec:` label or a Markdown link (`**Spec:** [SPEC-x](…/SPEC-x.md)`) too, not only with a backticked path after `**Spec:**`.
- The Spec and Plan tabs refetch their revisions and findings when you come back to them, flag a cut diff (and round its size up), keep your line comments in **Request changes** while you look at a diff, and list only the findings of their own phase or on their own files. A finding's line that is negative or longer than nine digits no longer breaks the findings panel.

## [2.8] - 2026-11-16

### Added

- Each run has its own page in the dashboard (`#/run/<id>/<tab>`), with a stage rail, a waiting banner, Overview, Activity, Gates and Files tabs, and only the buttons the run's state allows. A card's click opens it, and Space opens a short peek.
- `dashboard.board_refresh_s` (2 by default, 0.5–60): the most often the dashboard refetches the Board while events arrive.
- `/api/events?quiet=1` sends each agent session's turns as one `agent_activity` message (last tool, context %) at most every 2 s, the newest always delivered. The dashboard page uses it.
- `watchdog.no_progress_min` (0, off, by default; a finite number up to 1440) ends a step's agent session whose stream has been quiet for that many minutes and stops it as `step_timeout` (`watchdog_fired`), which a resolver may settle. While a tool call is open, and always for Codex, the limit is at least `defaults.gate_timeout_min`.
- `preflight.enabled` (off by default) checks the machine once before a run's first phase: each program of the lane's gates is on `PATH` and starts `--version`, Docker answers when a gate runs it, and the `setup` gate set passes. A failure is one `preflight_failed` stop before any agent runs, approved without a note because approving runs preflight again; a pass is cached for the day.
- A resolver may name its stop's cause in `stop_class` (`environment`, `flaky`, `guard_false_positive`, `spec_gap`, `impact`, `repeated_failure`), recorded once per session as `stop_classified`. For `flaky`, when the resolver changed nothing, the runner reruns each failed gate alone twice and records one `flaky:` run fact if they all pass. Each rerun is the whole gate, and the engine's resolve prompt asks for `stop_class` by default, so a `flaky` claim on a `verify` gate that runs the full suite costs two more runs of that suite. A stop whose resolve attempts are used is recorded as `repeated_failure`.
- A resolver's `environment` is checked by probing the step's gate programs. When the probes fail too, the stop becomes one `environment` inbox item, notified once per distinct problem, and again when it is back after an `approve --note` override. `approve` checks the machine again first (`--note` skips the check), `reject` is refused, and `agentflow queue` and the dashboard approve it themselves and resume the run once the checks pass (a run whose worktree is gone is left to a human).
- `ci.watch_max_h` (0, off, by default) watches CI checks still pending after `ci.timeout_min` for up to that many hours instead of stopping for a human: the run is held (`ci_pending`), told to a human once as needing nothing, frees its queue slot, and charges the time to `ci` in `metrics`. A `gh` error or no checks during the watch is no verdict; past the watch the `ci_failed` stop is raised as before. A new runner ends a hold its dead predecessor left open.

### Changed

- Dashboard live updates no longer rebuild what you are reading. Open sections, the scroll position and dialogs survive them. The Board refetches at most every `dashboard.board_refresh_s`, and an open Run page refetches only its own run.
- The dashboard's Board reuses an unchanged run's phase times, today's spend and time and cost, keyed by its event log's size and mtime, instead of recomputing them on every refresh; a live run's are recomputed at least every 30 s. Each refresh still loads every run's state from its log.
- `GET /api/runs/<id>` no longer embeds the run's whole `state.json`: it returns the fields the page shows, plus `actions` (what the run's state allows now), `label`, `stage_rail` and `live_agents`. `?raw=1` returns the old payload for debugging.
- A `blocked_repeat` stop that comes back after a human's approval says so in its notification and its inbox item, with how many approvals it has outlived (`times` on `hitl_raised` and in the wait). The stop and its refusal of a plain approval are unchanged.

## [2.7] - 2026-11-15

### Added

- Three throughput signals, `lead_time_h`, `human_blocked_share` and `full_suite_runs`, for the scorecard and lesson targets.
- `agentflow metrics` prints where each run's wall time went, as time and share per bucket, with its lead time, the share blocked on a human and its full-suite runs, and the same in total.
- The pull request body has a `Verification` section: each acceptance criterion with what ran to prove it, what verify could not run and why, and evidence older than a review fix marked as such. Each text verify wrote is shown as code, so it cannot render, link or mention.
- Synced issues that GitHub marks *blocked by* another issue show `Runs after:` / `Blocks:` on the Backlog and cannot start until the blocker is met, and `doctor` checks that `gh` can read issue dependencies.

### Changed

- `verify` proves the branch works and debugs a failing gate in its own step, with no fix plan of its own; a criterion it cannot run because it needs something outside the worktree (a browser, a running stack) is listed as not verified and is a `minor` finding, no longer a blocking one. `branch_review` judges five axes, checks verify's evidence under `## What verify proved` and no longer re-checks completeness or coverage. A pack override of `verify.md` or `branch_review.md` with the old text gets a `validate --fix` repair, which also adds that section.
- The per-task review in `build` (`@skills.map.review`, the bundled `agentflow-review` skill) also judges the five axes.

### Fixed

- A synced issue no longer shows `Ready` while an issue it is blocked by is still open.

## [2.6] - 2026-11-13

### Added

- `agentflow validate --fix [--dry-run]` repairs pack prompts that hold text the engine retired or that copy an engine prompt unchanged; it writes only `.agentflow/prompts/`, never commits, and refuses to run inside a run.
- `validate` warns about those prompts, marks a warning it can repair `(fixable)` and ends with `N fixable: run agentflow validate --fix`.
- A doubt review of the spec and the plan runs by default in the `feature` lane, before a human approves `define` or `plan`: the steps `spec_review`, `plan_review`, `spec_reconcile` and `plan_reconcile` are in the engine defaults, with the bundled `agentflow-doubt` skill (`skills.map.doubt`) and prompts.
- A reconcile round: the runner reopens the phase on the reviewer's `critical`, `high` and `medium` findings, runs the `reconciles: true` step and the review again, and stops when the artifact did not change, a disposition is disputed, two rounds fixed nothing or `autonomy.finding_rounds` is used up; the reconcile step may change only its `output`, which a guard and the commit check enforce (`outside_output`).
- The approval page and the handoff list each finding of a doubt review with its disposition and status.
- `validate` checks `reconciles` and `blocks_on_findings` steps, and the keys `phases.<name>.steps[].reconciles` and `skills.map.doubt` are protected in the Settings form.
- Signals `doubt_blocking`, `doubt_rounds`, `doubt_actionable`, `doubt_noise`, `doubt_misread` and `doubt_open`, per phase, from the new `doubt_settled` event, in `agentflow scorecard`, the Metrics tab and as lesson targets.

### Changed

- A step result whose `dispositions` is not a list is unreadable, as one whose `findings` is not a list already was.
- The runner's git calls for the snapshot, the commit and the reconcile checks run with `--no-replace-objects`, `core.hooksPath=/dev/null` and `core.fsmonitor=false`, so a user's own fsmonitor daemon is skipped, which can slow commits in a very large repository.
- The autonomy floor: `autonomy.finding_rounds` counts as at least 1, and findings of severity `critical`, `high` or `medium` (or with none) always block, whatever `autonomy.non_blocking_severities` lists. `validate` refuses a value below the floor, and the Settings form refuses one, so a pack that already sets such a value cannot save from the form until its YAML is fixed.

### Fixed

- `{date}` (so the `plan_target` file name) is pinned at run start in `params["date"]`, so a run resumed on another day keeps its plan path.
- Commits with explicit paths (the runner's `commit(paths=...)`) treat them as literal pathspecs (`--literal-pathspecs`), so a path with glob characters names only itself.
- `validate` reports a prompt that is not UTF-8 text as an error instead of stopping on a traceback.

## [2.5] - 2026-11-13

### Added

- The runner sizes every step before it starts, with a short read-only sizer session, and sizes a plan's build tasks and their reviews in chunks from the plan; a step goes up one level once, in the same session, on its first failed gate or a reopening review. `effort.sizing: false` turns it off.
- Events `sizer_started`, `sizer_finished`, `effort_decided`, `effort_escalated` and `plan_sized`, and signals `effort_escalations:<phase>`, `undersized:<phase>` and `sizer_fallbacks:<phase>`, in `agentflow scorecard`, the Metrics tab and as lesson targets.

### Changed

- Define, plan, review and debug steps never run below `medium`, and `validate` refuses an `effort.allowed` with nothing at or above `medium`.
- `validate` refuses the step ids `size` and `size-<n>`, which are reserved for the sizer's own sessions.

### Removed

- The in-session effort check: no prompt asks the agent to size its own step, and the `effort_change` status is gone from the prompts. A result that still sends one is refused with `effort_refused`.

## [2.4] - 2026-11-11

### Added

- `issues.sync_label`, `issues.sync_lane` and `issues.lane_labels`: the dashboard shows the open GitHub issues that carry the sync label as Backlog cards (`issue:<N>`), each in the lane its labels pick. One that carries the in-progress label with no run on this machine shows "Taken elsewhere" and can't start, so two people never work the same issue.
- A feature card can carry an issue number (`init` now writes `feature: [new_module, issue, after]` for a specs pack). Its run, like a synced issue's, labels and comments on the issue and links its PR into the issue's Development section. In the `module_lane`, an issue with no new module works on the module `issue-<N>`.

- The runner keeps the machine awake while it works (`caffeinate -i` on macOS, `systemd-inhibit` on Linux, tied to the runner's process) and lets go when the run stops for a human or ends; `agentflow doctor` has a `keep awake` check, and `AGENTFLOW_NO_KEEPAWAKE=1` turns it off.
- Signals `gate_minutes:<phase>`, `gate_reruns` and `refine_rounds:<phase>`, in `agentflow scorecard`, the Metrics tab and as lesson targets, so slow gates, gates run twice and runner-opened refine rounds show up on any repository.

### Changed

- A PR link to its issue that GitHub refused at `finish` is tried again on every CI round, not only at archive.
- The spec and plan prompts read the run's `issue.md` when the run comes from an issue.
- A gate that already passed in the run on exactly the tree being gated, with the same command and `defaults.env`, is reused instead of run again (`gates_reused`), across steps and phases; `gate_result` events now carry the command's `cmd` digest. Failures, skips and the `setup` set's gates always run again.
- In a fix attempt, the gates the last attempt failed run first, and the rest wait (`gates_deferred`) while one of them still fails; a step still finishes only once its whole gate set passed on its final tree.
- The plan prompt and skill keep whole-suite, other-module and end-to-end commands out of task steps, and the build prompt tells the implementer to skip such a step when a plan has one: the verify gates run them once for the branch.

### Fixed

- A refine round the runner opened on a phase that needs no human (such as `verify`) leaked into the next phase, which then re-planned and re-gated findings that were already settled. Approving a phase now ends its round.
- The verify prompt names `minor` for an observation that blocks nothing; a made-up severity such as `note` counted as blocking and opened a whole refine round.

## [2.3] - 2026-11-10

### Added

- A `policy` config: `policy.obligations` (merged by id; the engine ships `verified`, `reviewed`, `plan_approved` and `red_then_green`, and `engine_floor`, `gates:verify`, can't be removed) and `policy.enforce` (`report` or `block`). `validate` checks them, and the Settings page can't change them.
- A conformance audit at `finish`, before the push: it checks every obligation against the tree about to be pushed, runs the gates that have no passing result on that tree itself, and records `conformance_checked`. The draft PR gets a Conformance section (each obligation, its status, its evidence as `<run>#<seq>` and the tree). In `report` mode (the default) the run still pushes and the section starts `Conformance: N missing (report mode)`; in `block` mode a missing obligation stops the run with `conformance_failed` and nothing is pushed. `--dry-run` lists the obligations that would apply.
- `agentflow scorecard`: the finished runs' signals (gate first-pass, fix attempts, cost, wall and human-wait time, and the new `conformance_missing` and `conformance_waived`) against a baseline frozen with `--freeze` on this machine (`--replace` to refreeze); each signal reads `better`, `worse` or `same`, and any `conformance_missing` above 0 is an SLO breach listed first. The dashboard's Metrics tab shows the same table under Scorecard (`GET /api/scorecard`).
- A run started from an issue labels it in progress, comments on it, and removes the label when the run ends (or, failing that, when it is archived).
- An issue run's PR is always in the issue's Development section, linked through GitHub's API when `Closes #N` didn't link it (a base that isn't the default branch); an epic gets a comment with the PR's URL instead.

### Changed

- `agentflow retry <run> <step>` on a step of a phase that is already approved now runs that step again (it used to do nothing) and settles the phase again, and on a `conformance_failed` stop it also clears the stop, so `finish` audits again.
- An issue run's draft PR body says `Closes #N`, or `Refs #N` for an issue with an epic label.

### Fixed

- The dashboard's dialogs show why a request was refused: a New task the server rejects (a 400 or 409) now names the bad field inside the open dialog, where before the message was hidden under the modal and only the browser console showed `400 (Bad Request)`.
- The dashboard's terminal log names the reason for every refused request (`POST /api/backlog -> 400: <reason>`), not only its status.
- A Claude agent session's Bash tool now runs with the runner's `PATH`, the same one the gates get. Claude Code starts a login shell, and on macOS `path_helper` moved `/usr/local/bin` ahead of the rest, so an agent could find an older `node` or `python` than the gates and stop `blocked`. The runner writes the `PATH` to a `CLAUDE_ENV_FILE` script next to the step's transcript; a `CLAUDE_ENV_FILE` you set yourself is still sourced after it.
- A New task whose module id is a module that's already done says so and how to add work to it (unticked plan steps, `agentflow backlog undone`, or a new id), not "start module:<id> instead" for a module with no Start button.

## [2.2] - 2026-11-09

### Added

- Lessons and retros: every step's prompt carries the engine's and the pack's lessons (`agentflow/lessons.md`, `.agentflow/lessons.md`) through the new `{lessons}` slot, `agentflow lessons` shows each lesson's target signal before and after the runs that carried it, and `agentflow retro` (the `retro_step` session, run by `agentflow queue` when one is due once the first retro, run by hand, has finished) proposes lessons from the finished runs as a draft PR that changes only `.agentflow/lessons.md`. New keys `memory.lessons_max_tokens`, `memory.digest_tokens` and `memory.retro_every_days`; the dashboard gains a Lessons tab and a "Retro due" banner.

### Changed

- Run events carry more of what went wrong: `gate_result.tail`, `hitl_raised.extra`, `ci_round.failed` and `ci_round.tail`, and `step_retry.reason`, which the new `agentflow retry --reason` sets.
- The runner's `push` runs no repository hooks (a `pre-push` hook no longer runs) and pushes an explicit same-name refspec in every lane, so a `remote.origin.push` setting cannot send it elsewhere; a retro pushes its lessons commit by its sha.

## [2.1] - 2026-11-08

### Added

- A step's result may carry `code_map`: up to 8 lines naming existing code (`path:line`, signature, use). The run keeps the newest 40 for every later session.
- `gates.<name>.agent_check`: a gate the implementer runs itself before it finishes, so a lint failure is fixed in the same session instead of in a gate-fix round.
- `context.max_compactions` (default 3): a session that has auto-compacted that often is replaced by a fresh one seeded from `progress.md`. `validate` now checks the `context` limits.
- `agentflow init` finds a `docs/specs` folder (so the Backlog and "+ New task" are written for it), and sets `project.docs.spec_file` when most of its files are named `PREFIX-<module>.md`.
- The dashboard header shows the engine version (`v2.0`), served by the new `GET /api/info` route.
- This changelog, with a test that it has an entry for the current version.

### Changed

- The build prompts list a step's `agent_check` gates for the implementer to run before it finishes; with none, they read as before.
- Every session's prompt shows the run's code map, and the build prompts stop sending a fresh session back to the plan.
- The bundled plan skill asks for an `**Interfaces:**` list per task, naming the existing code the task calls by `path:line` and signature.

### Fixed

- Auto-compact now fires at 60% of a 200k window instead of 60% of the models' 1M window (600k tokens, so it never fired before the runner's 80% rollover). The runner sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW` from `context.context_window_tokens`, whose default is now 200000 instead of `auto`.

## [2.0] - 2026-11-08

The first changelogged release. It sums up the work from 2026-11-05 to 2026-11-08.

### Added

- Engine and pack split: the engine is project-agnostic and a repository's `.agentflow/` pack holds only config and prompt overrides.
- Installable tool: `pyproject.toml` with an `agentflow` console script, plus bundled generic stage skills.
- Config layers (engine defaults, starter, pack, personal user file) with null deletion and a protected-settings hold; `config show --effective` and the Settings page show each value's layer.
- Starters for python-uv, node-pnpm, go and docs-only repositories.
- `agentflow init` (detects the base branch, starter, gates, docs and skills and writes a sparse pack) and `agentflow doctor`.
- A `specs` work source that lists one module per spec file, with Backlog start, finished-module detection from the base branch on origin, and `agentflow backlog done|undone`.
- A soft run budget that only notifies and a hard run budget that stops the run for approval.
- Resolver sessions and refine rounds that take recommended answers in every phase except the human-only question kinds (default `boundary`).
- `SETUP.md`, an agent-driven setup guide with an update flow, and a GitHub Actions CI that runs ruff and the test suite.

### Changed

- Worktrees live in the state dir by default, and config layer changes are noticed during and across a run.
- A refine round redoes only the task whose stop was rejected, and skips the review of a task whose refine changed nothing.
- Agents and gates no longer see leaked environment variables (`defaults.env_inherit`).
- A build stops before its first task when a plan task names a forbidden path.

### Fixed

- `cancel` falls back when a run's recorded config no longer loads.
- `validate` reads an unreadable permission settings file as a warning instead of raising.
- Format characters are stripped from model text shown in the pull request body.
- A rollover test no longer races the adapter's SIGTERM.

[Unreleased]: https://github.com/anhhai680/agentflow-harness/compare/v2.0...HEAD
[2.0]: https://github.com/anhhai680/agentflow-harness/releases/tag/v2.0
