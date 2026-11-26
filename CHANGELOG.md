# Changelog

All notable changes to agentflow harness are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

The version is `pack.ENGINE_VERSION`; `pyproject.toml` must carry the same number (`test_packaging` checks it).

## [Unreleased]

## [3.14] - 2026-11-26

### Fixed
- A config file that would make the loader run for minutes is refused with a load error naming it: one longer than 128 KiB, one whose nested YAML aliases or `@` references expand to more than 100,000 values, and one nested more than 64 levels deep through its aliases (the CLI and the dashboard read the same loader, and the dashboard's Settings page refuses such a pack too).

## [3.13] - 2026-11-26

### Added
- `workspace.roots`, `workspace.repos` and `workspace.exclude` in your user config file name the repositories of your workspace: the git repositories directly under each root, the repositories you list, minus the exclusions. A pack that sets one gets a `validate` warning and the value is ignored, and the Settings page can't change them. `dashboard.board_refresh_s` and `dashboard.done_shown` are now personal keys too, so your user file may set them.
- `agentflow -C PATH <command>` runs a command as if started in `PATH`.
- `agentflow status`, `inbox`, `agents`, `next` and `doctor` take `--all`, which runs the command once in each repository of your workspace under a `== <name> (-C <path>) ==` heading, and prints `not set up` for a repository with no pack. `--all` is refused with `-C`, `--config`, `--watch` or a run argument, and the commands that write stay single-repository.
- `agentflow workspace list [--json]` shows the roots, each repository found (live or not set up) and each entry it skipped.
- `agentflow doctor` has a `workspace` check, shown only when your user file names a workspace, and `agentflow init` prints a note when the repository is not in it.
- The playbook has a section on several repositories (§17).

## [3.12] - 2026-11-26

### Added
- The playbook has a Backlog section (§16).
- `agentflow init` writes a `backlog` pack and an empty `backlog.yml` for a repository with no specs folder, and uses an existing `backlog.yml` as it is. A repository with a specs folder keeps the `specs` kind, and `init` offers `agentflow backlog import`.
- In a backlog pack, an item that names `issue: N` shows once on the Board, with the issue's link, and hides issue #N's synced card; the issue's in-progress label still marks the item as taken, and the issue's blockers still hold it.
- A synced GitHub issue that no item names has an **Add to backlog** button in a backlog pack's Board: it appends the item `issue-<N>` (status `backlog`, the issue's title and lane) to `backlog.yml` in your working tree. Only module-lane issues have it, and the button is not offered while the issue's start is in flight, while it is taken (in-progress label) or claimed on `origin`, or when an item already has the id `issue-<N>`.

### Changed
- `init` now writes `backlog.yml` as well as `.agentflow/config.yaml`, in the one case above; neither is written over anything or through a symlink.
- `validate` prints a note (not a warning, so `0 warnings` still holds) on a `capability_map` or `specs` work source, pointing to `agentflow backlog import`; both kinds are deprecated and 4.0 removes them.

## [3.11] - 2026-11-26

### Added
- In a backlog pack with an `origin` remote, starting an item claims it on `origin` (a one-file commit under `refs/agentflow/claims/<id>`, written with a leased push), and every phase and status change of its run moves the claim. A second start of a claimed item, by you or a teammate, is refused before any run, worktree or branch exists, and the message names the claimant. A start whose `origin` can't be reached is refused too, naming `backlog.claims: false`.
- `backlog.claims` (default `true`), `backlog.claim_ref` (`refs` or `branch`, for a host that refuses custom refs) and `backlog.claim_stale_h` (hours, default 24) set how items are claimed.
- `agentflow backlog release <id>` frees the claim on an item, whoever holds it (a stale or unreadable claim too). It works with `backlog.claims: false`.
- The Board draws other machines' claims as read-only cards in their phase column (who holds it, the phase and status, and a stale mark), and `agentflow backlog list` and `agentflow queue` show them, so `queue` starts no item a teammate claimed. A claim on an item that is `done` on `origin` is hidden.
- A run whose claim was freed or taken stops with `claim_lost`, naming the new claimant, and never pushes over it. A person settles it: `approve` claims the item again when it is free, `backlog release` frees the other claim, `cancel` ends the run. A failed push to an unreachable `origin` records `claim_sync_failed` and the run goes on.
- A claim is released when its run ends `failed` or `cancelled`, or is archived, and after a merge: the machine that ran an item frees its claim when it sees the item `done` on `origin/<fork_from>` (in the CI watch, the Board's refresh of its own runs, or `archive`). Reopening a `done` item (`backlog set`, or the Board's Edit) frees its leftover claim.
- `agentflow doctor` checks the claims on `origin` (a `claims` check, with one read-only `git ls-remote`, only with `backlog.claims` in a backlog pack).

### Changed
- `claim` is a reserved step id (the stop of a lost claim is `<phase>/claim`), and the Board's Edit of `backlog.yml` answers with `notes` about the claim of a reopened item.
- A claim push that `origin` refuses is tried once more before the start is refused, and the refusal shows git's own message; it suggests `backlog.claim_ref: branch` only as a maybe (credentials or read-only access refuse a push too), and never to a pack already on `branch`.
- When the release of a `failed`, `cancelled` or archived run's claim fails (say `origin` was unreachable), the claimant's next Board refresh or CI watch tries it again, so the item doesn't stay claimed.

## [3.10] - 2026-11-25

### Added
- In a backlog pack the runner writes `backlog.yml` on the run's branch, as its own commits: it appends a new or local-only item first (a local-only item's block as your working tree writes it, comments and unknown keys included), sets a `backlog` item `ready` when its run starts, records `spec` and `plan` after the step that wrote each, and sets `status: done` in `finish` before the push, so the PR's diff marks the item done. A run reads its item at its latest backlog commit. A gate's pass on the tree before the `done` commit counts for the tree after it. The events are `backlog_updated` and `backlog_skipped`; an item set `dropped` during the run stays dropped.
- The Board adds, edits and moves items of `backlog.yml` in your working tree (Add on "Not on the backlog", Edit and Move up/down on a card), and marks an item that differs from the file on `fork_from` with a `local` badge until you commit it. A write is refused with 409 when the file changed on disk since the page read it. `validate --fix` repairs "done by its runs".

### Changed
- In a backlog pack, agents can't edit `backlog.yml` in a run (its path joins `guardrails.forbidden_paths`), and the base sync settles the file itself: `fork_from`'s file with the run's own recorded changes made on it again, the only version of the file the merge commit lets through. A change to the file committed on the local `fork_from` and not pushed stops the sync instead of being dropped.
- A `module_lane` run of an id that is not an item of `backlog.yml` is refused before any run exists, and the New task form writes a new module to `backlog.yml` as a `ready` item in a backlog pack.

## [3.9] - 2026-11-24

### Added
- `work_source: {kind: backlog, path: backlog.yml}`: one `backlog.yml` holds every backlog item (`id`, `title`, `status` of `backlog`, `ready`, `done` or `dropped`, `lane`, `spec`, `plan`, `depends_on`, `issue`, `notes`), in priority order. An item's `spec` and `plan` replace the file-name rule, and in a run the file is read at the run's fork commit, never from its worktree. `backlog.ignore_specs` lists the spec-folder files that are docs and not items, and `validate` checks the file and warns about a Markdown spec no item names. This repository's pack now uses it.
- `agentflow backlog list [--all] [--json]`, `add`, `set <id> key=value …` and `import [--dry-run]` read and edit `backlog.yml` (comments and line endings stay as they are), and `import` writes the first file from the current work source.
- The Board shows backlog items with their title and an idea chip, lists `dropped` items under Dropped, and lists spec files no item names under Not on the backlog.
- `issues.sync_label: "*"` syncs every open GitHub issue to the Backlog, whatever its labels, and `issues.lane_labels` still picks each one's lane (so a `bug` issue is a bug card). Before, only issues carrying the one sync label were shown.

### Changed
- With a backlog pack, `agentflow backlog done` and `undone` edit the file's `status`, and `agentflow queue` starts only `ready` items and ignores `--include-specs` with a note.

## [3.8] - 2026-11-24

### Added
- `agentflow run vuln --advisory GHSA-…|CVE-… --package NAME --manifest PATH` starts a vuln run from GitHub's global advisory database, for a repository without Dependabot. It needs no token permission and works whatever `vuln.source` says. It excludes `--alert`, and is refused when no advisory names the package, the matches span more than one ecosystem or are all withdrawn, there is no patched version, or the severity is `unknown` or below `vuln.min_severity`. The PR's Security advisory section says `Advisory: <url>`, and the Backlog never starts one.
- `vuln.dependency_review` (default `false`, protected): once CI passes on the head a vuln run pushed, `ci` asks GitHub's dependency review API before it marks the PR ready. A new advisory at or above `vuln.min_severity` stops the run as `new_advisory`, and an API error, a malformed reply or a review that shows no change to the run's package stops it as `dependency_review_failed`. `agentflow approve` accepts that head (the PR says `Dependency review: accepted by <who> (<ids>)`), `retry` asks GitHub again and never accepts, and neither kind can be resolved by the runner itself. `validate` wants a bool, and with `true` wants `vuln` in `lanes`.

### Changed
- `doctor` makes one more read-only call, `gh api repos/{owner}/{repo}/dependency-graph/compare/<fork_from>...<fork_from>`, when `vuln.dependency_review` is on, `vuln` is in `lanes` and `gh` is on `PATH`; a `403` or `404` warns.

## [3.7] - 2026-11-24

### Added
- With `vuln.sync: true`, open Dependabot alerts at or above `vuln.min_severity` become dashboard Backlog cards, grouped by package and manifest (`alert:<lowest number>`). A card shows a severity pill, "N alerts · <manifest>", an open Dependabot PR line for each pull request naming the package, and an "Alert ↗" link; Start runs `vuln --alert N` with the lowest alert that has a patched version. A group with no patched version shows **No fix yet**, and one a vuln run holds shows **In progress** (a live run, or one stopped on an error, holds the package in the manifest; a done run with its PR open holds it only while one of its alerts is still open); both answer Start with 409. Alert cards can't be removed by hand, and a failed alerts call keeps the last list and shows an alerts note on the board and in the CLI board text (`alerts_note`), and a failed `gh pr list` call drops only the PR lines. Each refresh makes one alerts call and one `gh pr list` call.
- `validate` refuses `vuln.sync: true` without `vuln.source: dependabot`, `vuln` in `lanes` and a `vuln.probe`.

## [3.6] - 2026-11-23

### Added
- A `vuln` lane that patches a Dependabot alert's package, proving the advisory present before and gone after with the pack's audit probe (`--alert N`, `vuln:` config, `advisory_cleared` floor obligation). A triage session proposes the upgrade path and the `define` approval decides a major bump; scanner ignore files, scanner config, `dependabot.yml` and CI workflows are forbidden in the lane; `finish` binds the `advisory_cleared` clearance to the tree it pushes, and a later push (a CI fix round's commit, or the merge made when GitHub calls the pull request conflicting) is probed before it goes out. The PR gets a Security advisory section with the package's old and new version (`Target: <from> → <to>`, from the triage's `current_version` and `target_version`) and a `Probe:` line that says `cleared` only for a clearance of the head it shows. `vuln.dependency_files` names the files counted as dependency files: changes outside them are shown in the PR, never refused. A vuln run can't start `--from` a phase past `define`, which would skip the triage approval. `gates.<name>.ok_exit` and `found_exit` set a scanner's exit codes, `skills.map.patch` maps the patch skill, and a run's signals gain `advisory_reproduced`, `vuln_code_change` and `hours_to_pr:<severity>`.
- The starters (`python-uv`, `node-pnpm`, `go`) ship a pinned `audit` probe gate, `detect` and `init` turn an audit scanner step of the CI workflow into `gates.audit` and `vuln.probe`.
- `doctor` checks Dependabot alert access (one read-only `gh api` call) for a pack that can run the `vuln` lane; a `403` is a warning, since intake refuses such a token itself.

### Changed
- A step's `forbid` globs are now enforced at the runner's commit as well as at the step's guard check.

## [3.5] - 2026-11-23

### Fixed
- The dashboard header no longer breaks when the usage window carries a note: the note gets its own full-width line under the header's controls, so the run summary stays one readable block instead of a narrow column and the tab buttons stay on the first row.

## [3.4] - 2026-11-23

### Added
- `autonomy.speculate` (phases that run while the approval before them waits), `autonomy.approvals` (the `off`, `observe` or `auto` ramp for each phase approval, `goal` never `auto`) and `autonomy.ramp_samples` (3 to 50, default 5) are config keys with `validate` checks; `speculate` is a Settings form field, and the other two are protected. They are off by default, and with them off a run behaves as before; the lines below are what they do.
- With a phase in `autonomy.speculate`, the runner walks it while the approval before it waits (`plan` while the spec waits), once per revision and only when that approval's review left nothing blocking: the approval stays the run's wait, the walked phase settles at it (it never raises its own approval or approves itself), its steps may change only their `output`, and a stop in it is queued quietly behind the approval and told once it becomes the run's wait. Approving keeps the work and goes on, mid-walk too; rejecting ends the walk at the next step and runs the refine round; approving with `hold` ends it and rests the run as ready. A runner that died or was paused mid-walk goes on with it. Everything the walk left uncommitted is put back when the spec is rejected (and what a step's fence refused as soon as the walk stops on it), so `define`'s refine commit and later commits never take it (`speculation_started.dirty`, `speculation_ended.discarded`); a hold given while the walked phase refines or its stop waits is kept, so the run rests instead of going on into `build`; the runner never walks a phase a person already approved, or once a later phase started; and `validate` refuses a speculated `role: review` step whose agent wouldn't run it read-only. The approval page says the phase is being drafted, the approved phase's handoff lists only what it changed up to its approval, and the dashboard doesn't rest a run whose next phase is already being walked (`speculation_started`, `speculation_ended`).
- A phase walked behind an approval is re-planned once when the approved artifact changed under it: when the runner walks it again on a revised spec, or, when no walk ran on that revision, before it goes on after the approval. The re-plan is a runner refine round of kind `replan` (`phase_rejected` with `basis` and `from_sha`) whose note names the changed paths and the `git diff` to read, and it is not counted as a finding round. After a rejection, the re-raised approval page, the handoff and the approval phase's refine prompt leave out the files the walked phase committed, the walked phase's uncommitted `output` edits are put back before the refine round runs, and a round of the walked phase the rejection cut goes on as that round after the next raise.
- The approval ramp: for a phase that `autonomy.approvals` sets to `observe` or `auto`, the runner records `would_auto_approve` just before it raises the phase's approval, with the checks that held and those that didn't (`no_open_findings`, `small_route` for an XS or S route profile, `no_security_overlay`, `first_raise`). Under `auto` it approves the phase itself (`phase_approved` with `ramp`) when every check holds, the phase isn't `goal`, the last `ramp_samples` human decisions on the same lane, route profile and phase all agreed, no run it approved since then missed an obligation, a scorecard baseline exists and the lane's conformance isn't worse than it; otherwise a person is asked and the event says why. Conformance takes a ramp approval as the phase's approval unless the security overlay holds on the tree to be pushed (the stop's inbox then names the `agentflow retry` of the phase's review step that asks a person), and the ramp's own approvals never count towards its promotion. A run the ramp approved that is stopped at its audit counts against it at once, and a lane the baseline has no conformance for is never approved by the ramp.
- `agentflow scorecard` prints an `Approval ramp` table (lane, phase, profile, mode, human decisions, how many agreed, the ramp's own approvals, regressions and whether the phase is eligible, with the reason under a row that isn't), and a run's signals gain `replans`, `ramp_agreement:<phase>` and `auto_approvals`, present only for a run that speculated or was looked at by the ramp; a re-plan is no longer counted in `refine_rounds:<phase>`.

## [3.3] - 2026-11-22

### Fixed
- The bundled skills' `SKILL.md` frontmatter is valid YAML again: each `description` is quoted, so GitHub's preview and strict skill loaders no longer fail with "mapping values are not allowed in this context".

## [3.2] - 2026-11-22

### Added

- A board card shows a live run's slot number (a run that has ended shows none) and a badge for each parked step, with its summary as the badge title.
- `slots.count` (1 = off, at most 16) and `queue.human_slot` (default 1) are config keys with a Settings form section and `validate` checks, and `{slot}` is allowed in a gate's `affected.run` beside `{paths}`; `validate` warns when slots are on and nothing uses `{slot}`, or when `human_slot` is not below `count`. The keys do their work through the lines below, and with `slots.count: 1` a run behaves as before.
- With `slots.count` above 1, every run takes a slot number (`0` to `count - 1`) when it starts and keeps it while it is live (`slot_acquired`, the run's `state.json` `slot`), and `{slot}` in `defaults.env`, a gate's `run` and its `affected.run` is that number in the runner's config. A start with every slot held is refused before it creates a run or a worktree (`error: all N slots are held: ...`); a resumed run keeps its number unless `slots.count` was lowered below it, when it takes a free one (or is refused, changing nothing); and `agentflow status <run>` shows `slot:`. Slots can be turned on while runs are live: a live run that started with them off counts as holding `0` and takes its number at its next phase or resume, and a reload that finds no free slot is skipped (`config_reload_skipped`). With `slots.count: 1` nothing changes.
- With `slots.count` above 1, `agentflow queue` starts a run only while more than `queue.human_slot` slots are free, so a slot stays open for a run started by hand, and it reports `waiting for a free slot: ...` once per change; `--max-parallel` still caps it, and with slots off nothing changes.

### Changed

- A gate's `resource` now holds across the runs of a repository, not only within one run: the gate takes a file lock in the state dir's `locks/` around its command, on the plain path too, and a gate that had to wait records `resource_wait_s` on its `gate_result`.

## [3.1] - 2026-11-22

### Added

- Six signals read the waves of a build and join the vocabulary a lesson can target and the scorecard compares: `waves`, `parallel_demoted` (the tasks a wave turned away or set aside, a dissolve counted apart), `scope_violations`, `waves_dissolved` and `parallel_candidates` (the pairs in the run's last `parallel_candidates` event) are always present (0 with no wave), and `agent_parallelism` is the agent seconds of the build window (main sessions plus each sub-agent, from `subagent_started` to `subagent_finished`) over its wall seconds, present only when build had agent time, higher is better. The scorecard's baseline for them starts at the next `agentflow scorecard --freeze`.
- With `parallel.max_subagents` above 1 and the build phase on `claude`, the open tasks of one `**Parallel group:** <letter>` (a line a plan task carries under `**Depends on:**`, which the planner is asked to add to tasks that can be built at the same time, and the runner reads as `PlanTask.group`) that the runner proves independent (no dependency between them, their own files only, no serial path, no `risk` or security path, up to `max_subagents`; a file line's `path:12-30` range and leading `./` are cut, so two ranges of one file are a shared file) are built in one wave session (`build/build/wave-<n>/implement`, prompt `task_wave.md`) that dispatches one sub-agent per task card at once. The runner then keeps a changed path no member owns, and the work of a member that reported `blocked` or `needs_human`, sent no report or strayed (every member, when a stray is named by no report), out of the wave (put back as at the wave's start, uncommitted edits from before it included, and kept in the shelf ref `refs/agentflow/shelf/<run>/<prefix>.wave-<n>`, `scope_violation`), runs the build gates once on the combined tree (a red result reopens the wave session to fix it), and reviews and commits each kept task alone in plan order, each commit holding only that task's files. A task turned away (`parallel_demoted`) or set aside is built alone as before, and a grouped task joins no batch. New events: `wave_started`, `wave_attributed`, `scope_violation`, `parallel_demoted`, `wave_finished`. With waves off there is no wave event or behaviour; the prompt lines, the sub-agent event fields and the signals above still appear.
- A wave's kept members are reviewed by one read-only review wave session (`build/build/wave-<n>/review`, prompt `task_review_wave.md`) that dispatches one reviewer sub-agent per member at once, each with a card holding the member's own review prompt (`wave/wave-<n>/review-<m>.md`); its findings become each member's round-1 `review_result` (with `wave`, and the reviewer's `risks`, which the handoff lists as a review session's), and a member with blocking findings is fixed and re-reviewed alone as before. A member the session does not report on gets a review session of its own, as does every member when the session stops (`review_wave_dropped`, also for a session a runner died in), apart from a guard stop, which stays the run's wait. It runs for two members or more, never under a route profile with `wave_review: false` (the engine's `L`). A wave member's review range, in the review wave and in its own re-reviews, holds only its own files (`git diff <start> -- ':(literal)<file>'...`), and a path its own fix session changes outside its files goes back to how it was before the fix (`scope_violation`); once a stop of that fix is settled (answered, approved, retried or unparked) the check starts from the tree as it is then (`fix_rebased`), and it never touches the plan file or what the runner committed meanwhile.
- A wave whose session stops, dispatches a sub-agent the runner did not ask for (a task outside the wave, another model, an agent type other than `general-purpose`, a call that failed, a sub-agent unfinished when the session ended or one started in the background, since its call returns before it finishes) or not exactly one for each member in its first pass (one `guard_tripped` of kind `wave_dispatch`), leaves a stray from its gates' fix, cannot have its cards written (`cards`), or was found started and not done by a resumed runner once an agent of it ran (one paused before its first call starts fresh) is dissolved (`wave_dissolved`; a guard stop for a forbidden path is told to a person once first): its tree is kept in the shelf ref `refs/agentflow/shelf/<run>/<prefix>.wave-<n>.dissolved`, the paths it changed go back to the wave's start, and each member is built alone (`parallel_demoted`, `dissolved`); a wave stop where `HEAD` moved stays the run's wait, told once, as does any stop of a wave one of whose members already has its projected row or a commit (giving the wave up would undo it). A review wave whose reviewers were dispatched wrongly, or not exactly one per member it reviews, or whose cards cannot be written, is dropped (`review_wave_dropped`, `wave_dispatch`), a read-only review wave session gets the `Agent` and `Task` tools (and only it), allowed for `general-purpose` sub-agents only, and `subagent_finished` carries `ok: false` for a call that came back as an error.
- A build wave session's `max_budget_usd` is the build role's cap times its member count and a review wave session's the review role's cap times its member count; the timeout of either is `defaults.step_timeout_min` plus 15 minutes. A build wave session is never resumed after a stop or a killed runner (only the runner's own gate fix loop resumes it) and never rolled over, so a stop, a crash or a rollover of it dissolves the wave; each member's `wave_attributed` entry carries its one-line summary (`summaries`), which becomes its projected row's summary.
- A sub-agent's `subagent_started` and `subagent_finished` events carry its `description` (cut to 200 characters) and `background` (true when the call set `run_in_background`, any truthy value), and `subagent_finished` also carries `cost_usd`, estimated from the sub-agent's own stream lines at the session's prices.
- With `parallel.max_subagents` above 1, `agentflow explain` lists the waves the plan's `**Parallel group:**` lines would form (a `build/build/wave-1` line naming its tasks, only on the `claude` agent and with a plan), the plan's doubt review is told which ungrouped tasks could share a group (`{parallel_candidates}`) and to check that a group's tasks are independent, and the run records those pairs once as a `parallel_candidates` event; with waves off none of this appears, apart from the doubt review's new line asking it to check any group.
- `parallel.max_subagents` (1 = off, the default; at most 6) and `parallel.serial_paths` switch on waves of plan tasks built together by one session's sub-agents, with `skills.map.build_wave` (default `agentflow-build`) and a route profile's `wave_review` (`false` in the engine's `L`); `validate` checks them and warns when the build phase is not on `claude` or the pack's own `prompts/plan.md` never asks for a `Parallel group` line; the Settings form edits the two `parallel` keys; with waves off there is no wave event or behaviour; the prompt lines, the sub-agent event fields and the signals above still appear.
- `hitl.digest: true` turns each notification into one machine-wide line, such as "2 items need you (1 holds a run); 3 runs progressing", sent only when it differs from the last one sent (a digest that cannot be built falls back to the plain message, with a `notify_failed` event); it is off by default, has a `validate` check and a Settings form field.
- `hitl.park_kinds` names the stops a plan task may park on (`needs_human`, `blocked`, `gate_failed` and the other task stops; `[]` parks nothing, and a run-wide `budget_exceeded` or `environment` stop never parks), and `hitl.park_independent` now defaults to `true`; both have `validate` checks and Settings form fields.
- `agentflow approve <run> <key> --constraint` lifts a constraint gate in one step: with `guardrails.constraint_override` (`{trailer, gates}`, a trailer `reject_trailers` lists) set, a stop one of those gates failed on says so in its inbox, a person approves it, and the runner writes the override commit on `HEAD` itself, ending `<trailer>: <name>` (`--by`, else your login name; the dashboard's "Approve the constraint" also writes your login name, never `dashboard`), records `constraint_approved` and goes on with no `agent_commit` trip. The approval is refused for the runner's name, inside an agent session, for a stop with questions and when no constraint gate failed on the key; `finish` still refuses the trailer on any other commit. The key has `validate` checks and is protected on the Settings page.
- A parked task stop can be approved, answered or retried by its key (`agentflow approve <run> <key>`, `answer --key`, `retry <run> <key>`); settling one while the run waits on a promoted parked stop parks that one again, so the run goes on. `agentflow inbox` shows a parked stop's kind, its blast radius ("blocks task 2; task 3 continues") and its ways out (with `--note "..."` on the approve of a repeated block, as in its constraint line), and `reject` refuses a parked key until it is the run's wait. An `approve` or `answer` while a runner is live only records, and that runner picks it up.
- The dashboard Inbox shows a parked stop with its way on (an answer for one with questions, an approval for one without, its note rule following the stop that parked), an "Approve the constraint" button on a stop a constraint gate failed on (it arms on the first press and sends on the second, and `POST /api/runs/<id>/approve` takes `constraint: true`, judged against the run's own trusted config), and an "Approve all N" button in the approvals group that arms, then approves each phase approval that needs no note and was already shown when it armed (one that arrives in the 4 seconds is left for its own card).
- The dashboard's Plan tab (the run's plan view) shows a task that names a file a stopped task also names as waiting on it, as it already did for a task that depends on it; the same rule decides which tasks a parked stop holds back (its blast radius).
- Two park signals join the vocabulary a lesson can target (`parked_units`, the stops parked to go on with other tasks, and `park_resume_rework`, the parked tasks that then had an unshelve conflict settled or a step retried); both are always present, lower is better, and the scorecard's baseline for them starts at the next `agentflow scorecard --freeze`.

### Fixed

- A resumed step sent back for review fixes that has no session of its own (a wave member, whose work its wave's session did) starts fresh led by the note it was reopened with, which it lost before; this holds with waves off too.

### Changed

- Any stop of a plan task's own step now parks (not only a `needs_human` the agent marked `can_continue_without`) once the resolver could not clear it and some other task can still run: the runner shelves the task's uncommitted work in a ref (`refs/agentflow/shelf/<run>/<task>`, the task key with `/` written as `.`), puts those paths back to `HEAD`, and builds the tasks that neither depend on it nor name one of its files. The shelf is applied back before the task runs again; one that no longer applies changes nothing and stops on `<task>/unshelve`, and approving that goes on with the tree as you left it. The task's review then covers only its own work, from the `HEAD` the shelf was applied on (`unit_unparked` `head`), and a review session that stopped before the park starts fresh, with your note or answers. A run's shelf refs are kept past `finish`, so the work stays recoverable, and deleted when the run is archived. A forbidden-path guard stop that parks has its forbidden edit set aside (put back as at `HEAD`, kept in the shelf ref) instead of shelved, so approving it resumes the task cleanly. A stop stays a stop when nothing else can run and no other task is parked, for a guard stop where `HEAD` moved, and with `hitl.park_independent: false`; each stop's own notice is sent once (`hitl_notified`), and walking the run again or resuming it does not repeat it; a park sends its own `parked <key>: …` notice, and promoting a parked stop to the run's wait (again after a demotion) sends the "No runnable task remains" one. `verify` never starts while a task is parked.
- A stopped member of a batch of tasks (`step.batch`) no longer stops the run: it is split out of its batch (`plan_batched` with `split`, every other batch kept, so a resume sees it) and parks alone, later running with a review of its own, while the rest of its batch goes on in a fresh session under the batch's one review. A batch member held behind a parked task is split out the same way, so the batch's review covers only the members that ran. A batch's last member still stops, since its review is the only one of the members committed before it. Rejecting a stop of a task that was ever in a batch of several, split out since or not, refines the whole phase, so no member committed without a review of its own is passed over.

## [3.0] - 2026-11-20

### Added

- The usage-window governor (`usage.*`): above the soft mark no new run or phase starts, above the hard mark no new session; held runs go again closest to `finish` first; the dashboard header shows the window.
- `carry` on a `foreach: plan_task` step (`{max_tasks, max_context_pct}`, off by default: `{max_tasks: 1}`) lets the next task's implementer resume the previous task's session while that session is clean, below `max_context_pct` of its context (40 by default), not rolled over or compacted, and holds fewer than `max_tasks` tasks; a task the plan sizer flagged `risk` or sized above the session's effort starts fresh, every reviewer stays fresh and every task keeps its own commit. Each decision is recorded once as `session_carried` or `carry_declined` (with its reason). A bad `carry` is refused when the config loads.
- Every run session's call is capped by its role's turn and spend caps (`--max-turns`, `--max-budget-usd`; `run --dry-run` prints both). A capped review (`role: review`) stops at once as `turn_cap` or `budget_cap`, even with a result, so a truncated review is never taken as complete; a usage-window refusal is still a hold. A capped editing step with no valid result gets one wrap-up turn per approval (the same session, `--max-turns 3`, at most $2.50, a `session_capped` event), and stops as `turn_cap` or `budget_cap` when that turn does not finish it, with what is left to do (`signals.todo`, cleaned, one line each) and four ways out in the inbox: approve to raise the cap once, approve with a note to split or descope, or retry. A capped call never counts as a dead agent.
- A Claude call can pass a per-call spend cap (`--max-budget-usd`, from `AgentCall.budget_usd`) and gets the shell-output cap `BASH_MAX_OUTPUT_LENGTH` from `agents.claude.bash_output_chars` when that is above 0 (set by the runner, so `defaults.env` and the inherited environment can't raise it); a result that says the CLI stopped at a turn or spend cap is read as a cap (`AgentOutcome.capped`), not as a failed agent.
- A read-only session's first prompt tells it what it may do and not to stop `blocked` for a denial, and gives it a table of the gates the runner already ran on the run's work (the newest result per gate, up to 20, escaped for `|`); it is the new `{read_only_note}` prompt placeholder, empty for every other session.
- `agents.claude.read_only_roles` (`[review]` by default) and `watchdog.read_only_idle_min` (5) are new keys, with `validate` checks (a role not in `models`, a step of that role that edits the tree, a read-only flag, `--agent` included, in `base_args` or the `command`'s arguments, a non-Claude agent that lists roles, an idle limit above `no_progress_min`) and Settings form fields.
- Each Claude run session gets a run brief, a cached system prompt (`--append-system-prompt-file`) holding the repository's agent rules, the engine preamble, the step's skill and `brief:` files and the result contract, all read at the run's fork commit and never from a working tree; the user prompt keeps only what is specific to the step. A session is pinned to the brief it started with (`agent_started.brief`), every resume passes it, and a brief that can't be given again starts a fresh session (`brief_replaced`). A brief that can't be built or written gives none (`brief_skipped`), and rules it can't carry are left to Claude (`rules_fallback`). The retro, sizers and Codex get none.
- `context.run_brief` (on), `context.resume_gate_lines` (80) and `project.agent_rules` (`[CLAUDE.md]`) are new keys, with `validate` checks (including a warning for each memory file a run brief could not replace, `CLAUDE.local.md` and case variants such as `claude.md` included, for a tree at `HEAD` too large to list within bounds, and for a pack override of `_preamble.md` or `_result.md` that is not committed) and Settings form fields for the two `context` keys.
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
- `agents.claude.max_turns_by_role` (`{review: 45, plan: 60, build: 60, debug: 60, mechanical: 15}`, in the CLI's API-call unit), `agents.claude.session_budget_usd` (`{review: 4, plan: 10, build: 10, debug: 10}`) and `agents.claude.bash_output_chars` (15000) are new keys, and a step may set its own `max_turns`, each with `validate` checks (`0` is no cap, `null` removes an entry, a role must be in `models`, a warning on a non-claude agent; a step's `max_turns` that is not a whole number from 1, a `true` in either map or in `bash_output_chars`, and a spend cap too large for a number are refused when the config loads) and a Settings field. `--max-turns` and `--max-budget-usd` join the flags `validate` refuses in `command`, `base_args` and `effort_arg`, with the key that sets each; `autonomy.resolve_kinds` gains `turn_cap` and `budget_cap`.

### Changed

- The engine is 3.0, a major version: a pack's `engine:` range must include 3, and a pack that declares `>=2,<3` is refused when its config loads until the range is edited (to `>=3,<4`, which `agentflow init` now writes). The pack format stays `version: 2`. The reason is that the usage-window governor is on by default and holds runs and sessions that 2.x would have started.
- The prompts that told a session to run the build or verify gates before it finished (`task_inline`, `fix`, `ci_fix`, `fix_findings`, `sync_conflicts`) now say the runner runs them after the session, and to run only the tests it wrote, changed or covers; the fix turn's lead says to re-run only what failed, with quiet flags. The preamble's `## Shell` gains three lines (quiet flags for tests, Glob and Grep with a result limit instead of `grep -r`, `find` or `ls -R`, read a file once), and `agentflow-build` asks for quiet flags. `validate --fix` repairs a pack override that holds the old sentences (six new retired entries).
- A pack's `agents.<name>.max_turns` no longer caps the roles `agents.claude.max_turns_by_role` names (`review`, `plan`, `build`, `debug` and `mechanical`): they get the role caps, and `max_turns` stays the cap for every other role. The Settings page's agent note says so. Upgrade impact: a pack that sets `agents.<name>.max_turns` now gets the role caps for those roles, so a run may stop at a cap it did not hit before (set a role to `0`, or raise it, to keep the old behaviour); and a pack that removes a role from `agents.claude.models` (for example `models: {mechanical: null}`) must also set `max_turns_by_role.<role>: null` and `session_budget_usd.<role>: null`, because the engine's caps still name the role and `validate` errors on a role not in `models`.
- A doubt review after the first round (`spec_review`, `plan_review`) is shown the artifact's diff since the last review started, in front of the earlier findings and their dispositions, and gets half the resolved turn cap (the step's `max_turns`, else the role's `agents.<name>.max_turns_by_role` entry, else `agents.<name>.max_turns`; none when none is set or the role's entry is `0`). With no usable base (no earlier review in this round, an unreadable base, a deleted or new artifact, an empty diff or one over 400 lines), it gets the full prompt as before. The first round, the stop rules and the finding numbers are unchanged.
- Sessions of a role in `agents.claude.read_only_roles` (`review` by default, the retro included) now run read-only: `dontAsk` with only `Read`, `Grep`, `Glob`, `Bash` and `Write`, an allow list of read-only `git` commands and writes only in the run's workspace `out/` folder (where the result file goes; never `progress.md`), no MCP servers and no skills listing, so their prompts (and those of reconcile steps and the record step) name skill files instead of skills. A session keeps the setting it started with when it resumes, whatever the config says by then; a workspace path a permission rule can't name exactly runs the step as before and records `read_only_skipped` once per run. A read-only session with no tool call open ends after `watchdog.read_only_idle_min`. Its denials are recorded with `read_only: true`, left out of the `permission_denied:<tool>` signals, reported as by design and marked so in the retro digest. A reviewer that changes the tree anyway fails the step with `read_only_edit` (the stop quotes the paths and names at most 15), also when a later session of the step is no longer read-only (a rollover after a config edit, a restarted runner), and a retry is refused until those paths are reverted. **Upgrade impact:** a pack whose `role: review` step sets `output`, `gates`, `commit`, `expect`, `fix_loop` or `reconciles` now fails `validate`; a pack prompt that tells reviewers to run tests will see those commands denied; `agents.claude.read_only_roles: []` keeps the old behaviour.
- A resumed session gets a shorter prompt: a fix turn carries the gate output cut to `context.resume_gate_lines` (the first failure and the tail of each gate's output, with its log path), a refine round that resumes its session sends only the note, and a session that has the run brief gets a one-line result reminder instead of the result contract.
- The step's id, its result file's path and the run's notes (what it already found, the code map, the lessons) move from `_preamble.md` and `_result.md` into a new engine prompt, `_step_head.md`, which every session's prompt now carries between the two; the wording is the same except that the preamble and the result contract point at it. A pack's copy of either file keeps working, and `validate --fix` repairs an exact old copy (three new `Retired` entries). While `context.run_brief` is on, `validate` warns about a pack copy of either file that holds a placeholder a run's brief cannot fill, because the sessions of such a pack will get no brief. A resumed turn now starts with a one-line reminder that names the result file.
- Claude run sessions now use the new `agents.claude` keys: the 5-minute prompt cache by default (neither an inherited TTL variable nor the pack's `defaults.env` can override it), a lean prefix (`--exclude-dynamic-system-prompt-sections`, `--strict-mcp-config`, no auto-memory) and `--setting-sources project,local`, and sizer sessions drop the skills listing (`--disable-slash-commands`). **Upgrade impact:** runs stop loading your own Claude plugins, hooks, MCP servers, `env` and `apiKeyHelper` from your user settings; `agentflow doctor` flags what yours hold, and `agents.claude.user_settings: true` in your user file keeps the old behaviour. `validate` refuses `--setting-sources` in `agents.<name>.base_args`, since the runner sets it. A CLI that refuses one of these flags stops the step as `blocked`, naming the flag and the fix, instead of being held as an outage.
- The engine's defaults now use `sonnet` for the `plan` and `debug` fallback models and the define, plan and build phases' planning models, and turn the watchdog on at `watchdog.no_progress_min: 15`. A pack that states its own values is unaffected.
- Decisions waiting on you move from the Board's Needs-human list to an Inbox tab with a count in the header. Each card has a title in words, the inbox text with its CLI lines folded away, the facts and artifacts it needs, what a stop holds up, and forms in place of the browser's prompt boxes. `dashboard.inbox_legacy: true` brings the old list back.
- A Board card lists every live agent of its run, not just the newest, and the Agents tab groups agents by run, puts idle ones first and opens each one's transcript.
- A run's Activity tab tells its whole log as sentences ("Task 9 · implement finished: …", "You approved define"), filtered to Decisions, Agents, Gates, Commits or Stops. It folds agent turns into "Agent active 14 min, 37 tool calls", pages back to the first event, and shows each line's raw JSON on demand. The run detail no longer sends its last 300 raw events.

### Removed

- The run detail's 16 KB transcript tail (`transcript`, `transcript_file`); the Agents tab's transcript viewer replaces it.

### Fixed

- `validate` now refuses the permission flags and `--setting-sources` in `agents.<name>.command`'s arguments too, which reach every call as `base_args` do.
- The guards' tree snapshot no longer misses a same-size edit made in the second the index was last written: its copy of the index keeps the index's mtime, so git still re-reads racily clean entries.
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
