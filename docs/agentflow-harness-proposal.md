# Proposal: from agentflow (fixed workflow) to an agent harness

Status: proposed (2026-11-04)

## Context

agentflow can already take a feature from spec to a PR that is ready for review. It does this by walking one fixed, config-driven workflow: `define → plan → build → verify → review → ship`. A lane (`feature | bug | eval | design`) selects steps, and `conditions:` (file and git checks, never a model) decide what to skip. The original design said so outright: **"The agent never decides the next phase."**

That rule is the reason quality holds today. It is also the reason agentflow isn't a harness. The model can't choose a workflow that fits the task: it can't skip grilling for a one-line change, split a big task, add a spike, pick Opus for a hard task, re-plan after a failed approach, or handle work that fits no lane (refactor, dependency bump, docs, a cross-module epic).

The goal is to let the model decide **how** the work gets done while the harness keeps deciding **what "done" means**. Then the output can't fall below today's bar, whatever route the model picks.

Evidence from the first 10 real runs, made before slice P (`agentflow metrics --all`, events.jsonl):
- Cost runs $5–32 per feature, and build is 70–85% of it. Rework is $0.3–3 per run.
- Humans wait 0–64 min per phase. Most of that is approvals and stops.
- Compliance noise already shows up: 265 `permission_denied`, 154 `progress_not_updated`, 4 `footer_mismatch`, 18 `step_reopened`. Those signals already exist; nobody has turned them into a scorecard yet.

---

## 1. Core principle: "model proposes, harness disposes"

Split the work into three layers. Only the middle one is new freedom for the model.

```
┌───────────────────────────────────────────────────────────────────────────┐
│ 1. POLICY (deterministic, config.yaml, agent can't edit)                   │
│    obligations: "if the diff/task has property P, step S must have run    │
│    and passed on the FINAL tree", plus guards, budgets, approvals          │
├───────────────────────────────────────────────────────────────────────────┤
│ 2. CONDUCTOR (model)   chooses the route: which catalog actions, order,   │
│    model/effort, task split, batching, recovery, re-plans                  │
│    → emits a Route JSON. Never executes anything itself                    │
├───────────────────────────────────────────────────────────────────────────┤
│ 3. RUNNER (existing pipeline/lanes/gates/guards)                           │
│    validates the route against policy → executes one step per claude -p   │
│    → records evidence → conformance audit before ship                      │
└───────────────────────────────────────────────────────────────────────────┘
```

**Quality invariant:** `route = obligations(task, diff) ∪ model_choices`. The conductor can **add** rigor and **choose among equivalent options**. It can never remove an obligation. Obligations are recomputed from the **actual final diff**, not from the plan, so scope creep can't sneak past them.

---

## 1b. Portability: a project-agnostic engine plus a per-project pack

agentflow has been cut out of the project it was built in, and it will run on many projects. So the harness is split in two, and **nothing project-specific lives in the engine**.

```
agentflow (own repo, installed as a tool: `uv tool install agentflow`)     <project repo>/.agentflow/  (the "pack")
├── engine: runner, conductor, route validator, conformance,               ├── config.yaml   project, gates, gate_sets, conditions,
│   digest/memory, scorecard, dashboard, CLI, agent adapters                │                 overlays, catalog, policy, lanes, memory
├── generic events + reducer, generic KPIs                                  ├── prompts/      overrides (pack wins over engine default)
├── default prompts (placeholders only: {docs.*}, {brief}, {gate_evidence})├── lessons.md    project lessons
├── config schema → validate + Settings form + protected keys              ├── harness_eval/ route-eval cases + baseline.json
├── engine floor obligations (universal, see below)                        └── (runs/, workspaces/ git-ignored, or under ~/.agentflow/<repo-id>/)
├── engine lessons.md (model behaviour, e.g. "Sonnet needs a brief")
└── starter packs: python-uv, node-pnpm, docs-only (templates for `agentflow init`)
```

**What belongs where:**

| Concern | Engine (generic) | Pack (a project's values) |
|---|---|---|
| Workflow | Route schema, action catalog *mechanics* (`requires`/`provides`), lanes as data | Which actions exist, their skills and prompts, reference routes per lane |
| Quality bar | Obligation *engine*, conformance audit, evidence-by-tree-SHA | The obligations themselves (e.g. a `CONSTRAINTS.md`'s gates, security/eval overlays, spec/plan approvals) |
| Conditions | Generic checks (`file_exists`, `grep`, `section_items`, `status_line`, `changed_paths`, `plan_tasks`, `lane`, `gate_passed`, `command`) | Named conditions with the project's paths and patterns |
| Skills | `project.skills_dir` (default `.claude/skills`), resolution, brief injection | The skills, and which step uses which |
| Work intake | A **work-source interface**: `capability_map` (a markdown table), `github_issues`, `backlog_file` | Which source and its path (e.g. `docs/specs/CAPABILITY-MAP.md`) |
| Guardrails | Universal floor (below), deny-rule generation, guard checks | `forbidden_paths`, `reject_trailers`, `user_only_skills`, `human_paths` |
| Memory | Tiers, digest builder, caps, retro step, engine lessons | Project lessons, stats, ledger (per repo) |
| Metrics | KPI definitions from generic events, scorecard, autonomy ladder | SLO thresholds and baseline **per project** (projects aren't compared with each other) |
| Settings page | Form generated from the engine's config schema; engine-protected keys | Extra keys a pack marks protected (it can add to the list, never remove from it) |
| Agents | Adapters (`claude`, `codex`), price table | Which agent and which models per role |

**The engine floor**, the only obligations the engine imposes on every project, whatever the pack says:
- Only the runner commits and pushes. The runner never merges.
- Every diff passes the pack's declared `gate_sets.verify` on the final tree.
- No edits to the pack itself (`.agentflow/**`) or to the pack's `forbidden_paths` during a run.
- No user-only skills.
- Budget is a hard stop.
- 100% conformance before `finish`.

A pack can add obligations but can't remove these. `agentflow validate` refuses a pack with no `verify` gate set.

**Hard-coding removed in slice P** (found in this review; the paths are the source project's layout at the time):
- `.claude/skills` in `config.py:764`, `guards.py:120` and `lanes.py:138` → `project.skills_dir`.
- `devtools/agentflow/runs` and `workspaces` in `state.py:809-814` → `paths.state_dir`, default `~/.local/state/agentflow/<repo-id>/` (outside the repo; see 1c).
- The shipped-config path in `config_trust.py` → the pack's path.
- `devtools/bin/agentflow` → a console-script entry point.
- `module_queue.py`'s port of the source project's `next-step` skill and the capability-map parsing → the `capability_map` work source.
- The prompts that name the source project's code root, its docs, `CONSTRAINTS.md` and `floor_guard.py` (about 28 files in `prompts/`). Engine defaults become generic; a project's wording moves to `.agentflow/prompts/` overrides.
- The source project's `floor_before_push.py` stays there. The engine ships a generic `agentflow hook install` that runs the pack's `gate_sets.floor` (or a named gate) before push.

**Portability is enforced, not hoped for:**
- The engine's own test suite runs every feature against **two fixture packs** (a toy Python repo and a toy Node repo).
- A CI test fails if any engine file (code *or* default prompt) contains a pack-specific string (a denylist that includes `tivacare`, `CONSTRAINTS.md`, `src/`, `docs/specs`).
- The pack format is versioned (`version: 2`, `engine: ">=2,<3"`), and `agentflow migrate` upgrades a pack.

Everywhere below, a YAML example with a product repo's paths or skills (specs, ADRs, an eval suite) is **an example pack**, not engine code.

---

## 1c. Plug and play

"Plug and play" means plugging into any repo with near-zero setup: install, init, run.

### Install and run
```
uv tool install agentflow                 # or pipx; one CLI, the dashboard bundled
cd any-repo && agentflow init             # detect → write a sparse .agentflow/config.yaml → doctor → gate baseline
agentflow explain feature --task "…"      # what would run; no agent calls
agentflow run feature --task "…"          # first real run, at autonomy L0 (safe default)
```
- **`agentflow init` detects; it doesn't ask.** It reads:
  - the default branch from the git remote
  - languages and package managers (`pyproject.toml`, `package.json`/lockfile, `go.mod`, `Cargo.toml`)
  - gates, from the **CI workflow steps first** (`.github/workflows/*.yml`: CI is the project's real quality bar), then `package.json` scripts, `pyproject` tool sections and `Makefile` targets
  - an existing skills dir (`.claude/skills`), `CLAUDE.md`/`AGENTS.md`, and spec/ADR/plan dirs, if present

  It picks the matching starter (`python-uv`, `node-pnpm`, `go`, `docs-only`) and writes **only the values that differ from defaults**. A typical pack is about 15 lines.
- **The gate baseline is the one human touch.** `init` runs each detected gate once on the clean base branch and prints a table (gate, command, pass/fail, time). A gate that already fails on main is marked `baseline: red`, so a run can't be blamed for it, and the user fixes it or removes it. Detected gates are a *proposal*. `validate` warns until the pack has `gates_confirmed: true`, because the quality bar is a human decision, never an inference.
- **`agentflow doctor`** checks, with a fix hint for each:
  - the `claude` (or `codex`) CLI and its login
  - `gh` auth and a git remote
  - Docker, if a gate needs it
  - write access to the state dir
  - each gate's runnability
- **Built-in skills and prompts.** The engine bundles a minimal generic skill set, one per stage (define, plan, build with TDD, verify, review, ship), so a repo with **no** `.claude/skills` works on day one. A repo with its own skills maps them per stage (`skills.map: {plan: writing-plans, review: requesting-code-review, …}`). This repo keeps its vendored skills that way.
- **Leaves no footprint in the repo.**
  - Runs, workspaces, worktrees, memory and stats live in the user's state dir (`~/.local/state/agentflow/<repo-id>/`), not in the repo, so there is nothing to git-ignore.
  - Deny rules reach agents through the runner's `--settings` (as today), so the repo's `.claude/settings.json` needn't change.
  - The pre-push hook is opt-in (`agentflow hook install`).
  - Removing agentflow is `rm -rf .agentflow`.
- **Safe by default.** A new project starts every lane at autonomy **L0** (shadow): the conductor proposes and the reference route runs. The scorecard builds *this project's* baseline, and promotion comes after it is green (3.8). Plug-and-play never means trusting the model before it has a track record in that repo.

### Layered config: sparse files, full defaults
Precedence, highest first:
1. **CLI flags**
2. **user `~/.config/agentflow/config.yaml`**: personal settings such as models, budget and notifications. It can't touch gates, policy or guardrails.
3. **project `.agentflow/config.yaml`**: committed, shared by the team.
4. **starter defaults**
5. **engine defaults**

`agentflow config show --effective` prints every value with the layer it came from. The dashboard Settings form (3.9) shows the effective value and its source. Saving writes to the project layer, or to the user layer for personal keys. Protected keys are only editable in the project file, through git. `agentflow config diff-defaults` shows what a pack overrides, so upgrades stay easy.

**A pack is config, never code.** A pack holds values, prompts and plain gate commands only. It can't ship or import code that the runner executes, so a PR in the repo can't change runner behaviour.

### Multi-project
One `agentflow dashboard` serves every repo registered on the machine (`agentflow init` registers it). Each repo has its own Board, Scorecard, memory and baseline. `agentflow status --all-projects` gives the cross-repo view. Budgets and autonomy levels are always per project.

### Deviations in slice PnP (as built)
Slice PnP shipped in two plans (§5). Where it differs from the text above:

From PnP-1 (`docs/plans/2026-11-05-agentflow-harness-slice-pnp-1.md`):
1. **The bundled skills** are define, plan, build, debug, verify, review and respond (taking review feedback). `ship` is not bundled, because no agent step ships: `finish` is runner code.
2. **The engine defaults carry no `permission_mode`.** A pack states `agents.claude.permission_mode: bypassPermissions` itself, and `init` writes it with a comment, so the team reviews it in the pack's PR. Decided 2026-11-06.
3. **The bug lane on a repository with no Bash allow rule.** Its red/green check (`bug_eval.py`, through `guards.command_allowed`) needs a Bash allow rule that covers the test command, whatever `permission_mode` says. A sparse pack's repository usually has none, so `validate` warns about it; `guards.py` is unchanged. Decided 2026-11-06.

From PnP-2 (`docs/plans/2026-11-06-agentflow-harness-slice-pnp-2.md`):
1. **`init` runs no gate without `--baseline`.** Detected gates are a proposal, and nothing from the repository runs until a human asks. The gate baseline above is opt-in. Decided 2026-11-06.
2. **`init` writes the permission line itself:** `agents.claude.permission_mode: bypassPermissions`, with a comment. Decided 2026-11-06.
3. **The bug lane needs a Bash allow rule, and `init` prints one,** built narrowly from the first verify gate's command. Decided 2026-11-06.
4. **The Portability check** (`init --starter python-uv` in an empty repo, then `validate` reports 0 warnings) is read together with the `gates_confirmed` rule: `validate` reports exactly the `gates_confirmed` warning, and 0 once it is set. With PnP-1's third deviation, both this check and the "0 warnings after `gates_confirmed`" of the plug-and-play test hold once the human has also added the Bash rule `init` printed (the test puts it in the git-ignored `.claude/settings.local.json`, so the repository still shows only `.agentflow/`).

Not built in this slice: the multi-project registry and `status --all-projects`, `agentflow hook install`, `migrate`, `config diff-defaults`, `agentflow baseline` on an existing pack, Settings saving to the user layer, autonomy levels and the conductor, and a Rust starter. `init` does not map a repository's own skills into `skills.map`.

**Time to first dry run: not measured.** The plug-and-play test (`agentflow/tests/test_plug_and_play.py`) proves the path from `init` to `run --dry-run` on the three fixture repositories, but nobody has timed it on a real repository against the 10-minute target.

---

## 1d. Team backlog

Slice TB-A1 (`docs/plans/2026-11-07-agentflow-harness-slice-tb-a1.md`) lets a team start work from its own spec files. A pack may set `work_source: {kind: specs}`, which lists one module per file in `project.docs.specs` that fits `project.docs.spec_file`. The Backlog shows each module as a card and starts it in `dashboard.backlog.module_lane`, and a new feature card writes the module's spec file.

Slice TB-A2 (`docs/plans/2026-11-07-agentflow-harness-slice-tb-a2.md`) lets the Backlog recognise modules that are already finished, show the reason, and let a person mark or unmark them.

### Deviations in slice TB-A1 (as built)
1. **The spec header's `Depends on:` line** can be bold or plain, with a colon, and its bullets or a comma continuation count. An unknown, unreadable or cut dependency blocks the module.
2. **The task of a new feature card** carries the card's dependencies as a `Depends on:` line, and the spec prompt gains two sentences, not one. A new spec's dependencies are kept only when the define agent writes them into the header. Until then the card blocks on them, and the spec approval checkpoint shows them to the person who approves.
3. **`--new-module` in a `specs` pack** is refused by `_check_new_module`, which points to `--module --task`. `add_item` is a no-op for `specs`.
4. **`init` writes the `specs` source** only when `detect` finds a top-level `specs/`, `docs/rfcs` or `docs/proposals`. A repository with only `docs/`, or with `docs/specs/`, gets no `specs` source. For `docs/specs/`, `detect` prints a note, and the source is a manual addition.
5. **Files that are not modules** are skipped: non-slug ids, symlinks, directories and unreadable files. Under the bare `{module}.md` pattern, `index`, `readme`, `template` and `changelog` are skipped too.
6. **`work_source.path`:** a `specs` source takes none, and always reads `project.docs.specs`.
7. **`agentflow queue` on a `specs` pack** needs `--include-specs`. Each run still runs its `define` and `plan` agent steps before it stops for approval.
8. **Modules finished before agentflow:** in slice TB-A1, such a module shows as startable unless its plan is finished and its branch is merged, which is today's rule. Slice TB-A2 changes this (see its deviations below).
9. **No engine version bump.** An older 2.x engine refuses `kind: specs` loudly in `validate`.
10. **No shipped-slice proposals to mark.** The "mark the shipped slices' proposals done" step has nothing to do here: this repository has one proposal, and it is still in progress.

### Deviations in slice TB-A2 (as built)
1. **Hand marks and vetoes** live in `runs/done_marks.json` (`agentflow/finished.py`), not in `backlog.jsonl`. `agentflow backlog done` marks a module done, and `agentflow backlog undone` removes a mark, or vetoes origin evidence.
2. **Finished work is read from `refs/remotes/origin/<fork_from>`**, the branch pull requests merge into, not from `origin/<base_branch>` or from the work tree. Unpushed commits on `fork_from` don't count. Merged-branch detection and hand marks work with no `origin`. In a repository with no readable `origin/<fork_from>`, a module already recorded as `merged` (or as done by origin evidence) stays done: a newly opened plan doesn't reopen it, and a veto doesn't reopen a `merged` one. Only editing `queue.json` does.
3. **The cheap reasons come before `gh`.** These rules apply to `capability_map` packs too: (a) a hand mark makes a module done, and a veto removes only a Done that origin evidence gave; (b) a spec header status on origin that says done makes it done; (c) a newest dated plan on origin makes it done with no `gh` call when it is fully ticked, names the module's spec file on its `**Spec:**` line, and both origin listings were complete; (d) `modules_done` counts every dependency the last assessment recorded as done, whatever the reason, and asks `gh` only for the rest; (e) `agentflow next` skips done modules in `rule_spec_without_plan` and `rule_checked_plan`; (f) a plan belongs to a module by its dated name and the longest known id; (g) done entries in `queue.json` gain `done_reason`.
4. **A plan belongs to a module** by its dated name, the longest known id and, for the no-`gh` rule, its `**Spec:**` line. The other `next` rules still use `find_plan`.
5. **The reason words** are `done by hand`, `spec says done`, `plan complete on <branch>` and `merged`. They replace the spec's "merged PR" and "plan complete".
6. **The default `done_status`** accepts bold, list and quote forms and matches header lines only. It still accepts a trailing qualifier such as `Status: done pending review`; a veto fixes such a case. A header line cut at 512 characters never counts as saying done.
7. **A veto ignores only origin evidence** (the spec says done, or the plan is complete on origin). A module already done as `merged` stays done after a veto, in every view, because a merged branch is not origin evidence.
8. **An unreadable `runs/done_marks.json`:** hand marks recorded in `queue.json` stay done. A veto cannot be read, so origin evidence creates no new Done until the file is repaired, and the queue log says so. The CLI refuses to change marks and prints the repair text.
9. **Origin that cannot be read in full** (no `origin`, git fails or times out, or a listing is over the count or byte limit): a module recorded earlier as done by origin evidence, or as `merged`, stays done. Nothing new becomes done from origin evidence on a partial listing. The one exception is the module's own spec file, as far as it was read (its last, possibly cut, line is dropped), which can still say done.
10. **`modules_done` and `agentflow next`** read what the last assessment recorded, and the dashboard refreshes it every 60 s (`REFRESH_EVERY_S`) while `agentflow queue` refreshes it every `--interval` (default 30 s, `INTERVAL_S`). With neither running, only the hand marks are current.
11. **In TB-A2, the module list is still read from the work tree,** and the fresh re-check before a start (spec B4) isn't built. Both move to PR B.
12. **An approved spec with no plan,** finished before agentflow, needs `agentflow backlog done`.

This repository's pack turns the source on (`work_source: {kind: specs}`, `module_lane: feature`, and a `feature` New task form), so `harness` is a Backlog card.

---

## 2. What is missing (gap analysis)

| # | Missing piece | Today | Needed for a harness |
|---|---|---|---|
| G1 | **Action catalog** | Steps live inside fixed phases; a lane is a filter | Steps become reusable actions, each with `requires` / `provides` / `cost` / `risk`, which the conductor composes |
| G2 | **Conductor (decision-maker)** | The runner's loop picks the next step from a list | A model session at *decision points* that returns a typed `Route` / `NextAction` with a rationale |
| G3 | **Policy-as-code obligations** | Implicit: whichever steps happen to be in the lane | Explicit rules: diff/task property → required evidence (gate, review, approval), checked mechanically |
| G4 | **Route validator** | n/a (routes are fixed) | Rejects a route that is missing obligations, uses an unknown action, a user-only skill, or a forbidden path, or exceeds budget |
| G5 | **Conformance audit (evidence bound to tree SHA)** | Gates run per step; nothing proves "verify ran on the tree we push" | Before `finish`: every obligation has a *passing* event recorded against the final tree hash, or the runner runs it itself |
| G6 | **Dynamic re-planning** | The resolver fixes a stop, but the route stays fixed | At failure or surprise, the conductor may re-plan (split a task, spike, switch model). That is bounded and logged |
| G7 | **Long-running goal runs** | One run = one module/issue; `queue` starts modules in map order | A *goal* run (epic) that decomposes into child runs, keeps a goal ledger, and survives days (hold/resume already exist) |
| G8 | **Harness scorecard + eval suite** | `metrics.py` covers time/cost only | Quality, compliance and efficiency KPIs against a frozen baseline, plus a route-eval suite that gates conductor prompt changes (like the product's eval gate) |
| G9 | **Autonomy ladder** | Fixed `needs_approval` / `resolve` per phase | Per-lane autonomy level, promoted by measured results, auto-demoted on regression |

---

## 3. Design of each piece

### 3.1 Action catalog (`config.yaml: catalog:`), from G1
Recast the existing steps as named actions without changing what they do. The engine defines the action *schema* and ships a generic starter catalog. Each pack lists its own actions, skills and prompts. Example (an example pack):
```yaml
catalog:
  grill_spec:   {skill: grilling, role: plan, prompt: grill_spec_questions.md, provides: [spec_questions_resolved], human: answers}
  write_plan:   {skill: writing-plans, role: plan, provides: [plan], requires: [spec_approved]}
  implement:    {skill: subagent-driven-development, role: build, foreach: plan_task, gates: build, review: task_review, provides: [code]}
  spike:        {skill: systematic-debugging, role: plan, read_only: true, provides: [finding]}      # new
  verify:       {..., gates: [verify, web_e2e], provides: [verified]}
  branch_review:{..., provides: [reviewed]}
  security_review: {..., provides: [security_reviewed]}
  eval_gate:    {type: gate, gates: [floor, eval], provides: [eval_passed]}
  finish: ...
```
The current lanes become **reference routes**, the exact sequences that run today. They are the fallback, and the baseline everything is compared against.

### 3.2 Policy and obligations (`config.yaml: policy:`), from G3
The engine evaluates obligations generically. The list below is **an example pack**: another project declares its own, on top of the engine floor (1b). Evidence kinds are generic (`gates:<set>`, `review:<action>`, `approval:<phase>`, `ci_green`, `expect:<red|green>`). They are built from the pack's named `conditions:` (reuse `conditions.evaluate` in `agentflow/conditions.py`):
```yaml
policy:
  obligations:
    - {when: diff_touches_code,  require: [gates:verify, branch_review], after: last_code_change}
    - {when: security_path,      require: [security_review, approval:plan], human_skill: doubt-driven-development}
    - {when: eval_required,      require: [eval_gate], after: last_code_change}
    - {when: spec_changed,       require: [approval:define]}
    - {when: plan_exists,        require: [approval:plan]}           # spec/plan stay human-approved
    - {when: bug_lane_or_bug_task, require: [regression_test:red_then_green]}
    - {when: always,             require: [gates:floor, ci_green]}
  forbid_actions_by_conductor: [user_only_skills, edit_config, merge, lower_baseline]
  limits: {replans_per_run: 3, conductor_budget_share: 0.10, max_child_runs: 6}
```
`policy`, `catalog` and `guardrails` all go on the protected list in `config_edit.py` and on `guardrails.forbidden_paths` (config.yaml is already there).

### 3.3 Conductor (`conductor.py`, prompt `prompts/conductor.md`), from G2 and G6
- It is an Opus session that is **read-only** (role `review`: no edits, no Bash writes). Its input is the task, the repo state (`agentflow explain` output), the catalog, the obligations computed so far, the budget left, recent events, and a curated `lessons.md`.
- Its output is a JSON `Route` (an ordered list of actions with params: model, effort, batching, task split, plus `rationale`, `risks`, and `alternatives_considered`).
- **Decision points only** (keeps cost bounded at ≤10% of the run budget): at run start (route), after `plan` approval (build strategy), on a resolver failure or a second gate failure (re-plan), and before `ship` (anything missing?). Between those points the runner executes as it does today.
- If the conductor fails, returns invalid output twice, or exceeds its share, the runner falls back to the **reference route**. The worst case is today's behaviour.

### 3.4 Route validator (`route.py`), from G4
This is deterministic. It checks that the route covers `obligations(task)`, that every action is in the catalog, that no user-only skill or forbidden action appears, that `requires`/`provides` form a valid order, and that the estimate fits the budget. A rejection is recorded (`route_rejected`, with the rule that failed) and sent back to the conductor once, then the reference route is used.

### 3.5 Conformance audit (`conformance.py`), from G5. This is the key quality guarantee
Before `finish` pushes, the audit runs these checks:
1. Recompute obligations from `git diff origin/main...HEAD` (the real diff).
2. For each obligation, look in `events.jsonl` for a passing `gate_result` / `review_result` / `phase_approved` whose `tree_sha` equals the final tree (or that came after the last code change, for reviews).
3. If evidence is missing or stale, the **runner** queues the required action itself. The model is never asked whether to do it. Ship goes ahead only at 100% conformance.
4. The PR body gets a **Conformance table** (obligation, evidence event seq, tree SHA). This extends the existing "PR is the audit trail".

This needs one small change to existing code: put `tree_sha` (from `guards.snapshot`, `guards.py:43`) on every `gate_result` and `review_result` event.

### 3.6 Long-running goal runs, from G7

#### Where a goal comes from: the harness never invents one
**A human gives the goal, and the project's own documents ground it. Memory never does.** It follows the same rule as good escalation logic (act on observable signals, never on a model's self-reported confidence): the harness doesn't ask the model whether it's confident. It checks citations.

| Input | Role in a goal | Can it *define* the goal? |
|---|---|---|
| **The human's request** (`agentflow goal "<objective>" --ref <doc#section> --ref <issue>`) or a Backlog card | **Intent**: what is wanted | **Yes**, and it is the only thing that can |
| **Knowledge sources**: the pack's authoritative docs and trackers, ranked (`knowledge.sources`) | **Grounding**: what the intent means here, and what "done" looks like | It refines and bounds the goal; it doesn't start one |
| **Memory**: digest, ledger, lessons, stats (3.10) | **How**: route, cost, model choice | **No.** Memory is episodic and procedural, never a source of scope or acceptance |
| **The model** | Drafts the Goal Brief and the decomposition | **No.** It proposes; citations and a human decide |

**`knowledge.sources`** (pack, protected) declares where truth lives and its authority order. A higher source wins a conflict. An example pack:
```yaml
knowledge:
  sources:                                   # highest authority first
    - {id: idea,   path: docs/idea.md,                               cite: section}   # "the source of truth"
    - {id: adr,    path: "docs/decisions/ADR-*.md",                  cite: file}
    - {id: spec,   path: "docs/specs/SPEC-*.md",                     cite: section, ready_when: spec_is_approved}
    - {id: map,    path: docs/specs/CAPABILITY-MAP.md,               cite: row}
    - {id: issue,  tracker: github,                                  cite: number}
    - {id: rules,  path: [CLAUDE.md, CONSTRAINTS.md],                cite: section}
```
A generic starter defaults to `README.md`, `docs/**` and GitHub issues. `agentflow init` detects whatever exists.

#### The Goal Brief: a contract, written before any spend on building
1. **Intake.** The human gives the objective and, optionally, references.
2. **Draft (conductor, read-only, cheap).** It writes `goal.md` with:
   - objective
   - in-scope / out-of-scope
   - **acceptance criteria**
   - child work items, with dependencies
   - budget and deadline

   **Every** scope item, criterion and child item must carry a citation `[source-id: path#heading | row | #issue]`. Anything it can't cite goes in an **Assumptions** list, never mixed into scope.
3. **Grounding check (runner, deterministic, no model).**
   - Every citation resolves: the file exists, the heading/row/issue exists, and the issue is open.
   - Each cited excerpt is stored with the source's **content hash**.
   - Cited sources are checked for blockers: a spec with open questions or "pending approval" (the existing `spec_has_open_questions` / `spec_awaits_approval`), an unmerged dependency (`deps_merged`).
   - Two cited sources that disagree are listed, and the higher authority is proposed as the winner.
4. **Readiness score**, computed by the runner from observable facts, never from the model's self-rating:
   - citation coverage (cited items / all items; required **100%** for scope and criteria)
   - citation validity (100%)
   - open assumptions
   - unresolved conflicts
   - blocked sources

   The goal is **ready** only when coverage and validity are 100% and assumptions, conflicts and blockers are each 0 or answered.
5. **Questions, not guesses.** Each assumption, conflict or blocker becomes a question with a recommended option and its citation (the existing `needs_human` inbox). An answer that settles a spec gap is recorded in the source itself, by the existing `record_step` / `domain-modeling` flow. The knowledge improves and the goal stays grounded in it.
6. **Human approval of the Goal Brief is an engine-floor obligation** (`approval:goal`), the same in every project and never skippable at any autonomy level. The approved brief is frozen: its hash and the content hashes of its sources are stored in the `goal_approved` event.

#### Staying on the right goal over days
- **Source drift.** Before each child run and at each decision point, the runner re-hashes the cited sections. A changed source raises `goal_source_changed`. The affected children pause, and the conductor proposes an amendment that goes through steps 2–6 again.
- **Scope drift.** A child run's diff is compared with the goal's scope (paths and modules from the cited items). Work outside it is a `goal_scope_drift` finding that blocks that child's ship until a human accepts or rejects it.
- **Amendments only through a human.** The conductor may *propose* adding, dropping or reordering children (`goal_amendment_proposed`). Only `agentflow approve <goal> amendment-<n>` changes the brief. Re-planning *how* (routes) stays the conductor's.
- **Done is checked, not declared.** Each acceptance criterion compiles to a check where it can: a named condition, a gate, a module merged (`branch_merged`), or a spec status. The rest are marked `human_acceptance`. The goal finishes only when every check passes and the human accepts the rest. It never finishes because the model says so.
- The goal run sleeps between children. It is woken by `branch_merged` and by child-run events, rides out holds and outages (`recovery:`), and is bounded by the goal budget and deadline (3.11). Each child run gets its own policy, validation and conformance, and child runs never merge.

**Suggesting goals is allowed; starting them isn't.** From the work source (capability-map modules with merged dependencies, labelled issues), the harness may put **suggested** goal cards in Backlog, each with its citations and readiness score. As today, nothing starts until a human pulls the card.

**Measured** (Scorecard):
- citation coverage and validity at draft
- assumptions per brief, and the share answered by a human vs by the recommended option
- conflicts found
- amendments per goal
- `goal_source_changed` and `goal_scope_drift` counts
- acceptance criteria that are auto-checked vs human
- goals accepted at the first human review

**Settings (3.9):**
- `knowledge.sources` is **protected**, because it defines authority.
- `goal.require_coverage` is fixed at 100% for scope and criteria and not exposed.
- `goal.max_children` and `goal.max_assumptions_before_stop` are in the **Form** (tighten-only, like the budget thresholds).

### 3.7 Guardrail enforcement in depth (what keeps the agent following the rules)
| Layer | When | Mechanism | Status |
|---|---|---|---|
| L0 Static | Config load | `agentflow validate` checks catalog, policy and route schema | extend |
| L1 Pre-execution | Route proposed | Route validator (3.4) | new |
| L2 In-session | Every tool call | Deny rules, `--settings` denies, PreToolUse hooks (`floor_before_push.py`), `RUNNER_ONLY_DENY` | exists |
| L3 Post-step | Every step | `guards.check_step` (forbidden paths, HEAD moved, agent commits, trailers, user-only skills), gates, fix loop | exists |
| L4 Pre-ship | Before `finish` | Conformance audit (3.5) | new |
| L5 Post-PR | CI and human | CI, human PR review, never merge | exists |
| L6 Retro | Weekly | Scorecard and drift alerts (section 4) | new |

The principle: **the model's compliance is never trusted, only verified.** Every rule either can't be broken (L2), is detected right away (L3), or blocks shipping (L4).

### 3.8 Autonomy ladder (per lane), from G9
| Level | Conductor may | Promotion gate |
|---|---|---|
| L0 Shadow | Propose a route; the reference route runs | baseline captured |
| L1 Tune | Choose model/effort, batching, task order | ≥10 runs, no KPI regression |
| L2 Shape | Add or skip optional actions (grill, spike, per-task review on trivial tasks*), split tasks | ≥10 runs at L1 green |
| L3 Re-plan | Re-plan mid-run on failure | ≥10 runs at L2 green |
| L4 Goal | Goal runs with child decomposition | human decision |

\*An action that an obligation covers is never optional. Promotion is a human's decision (`autonomy.level.<lane>` in config). **Demotion is automatic** when a scorecard SLO breaks.

### 3.9 Dashboard Settings stay in step with config.yaml
Every slice that adds or changes a `config.yaml` key also updates the dashboard Settings tab **in the same PR**. To stay project-agnostic, the Form sections and the engine's protected keys move from hand-written lists in `config_edit.py` (`_SECTIONS`, `_PROTECTED_KEYS`) into the engine's **config schema**. The form, `agentflow validate` and the protected-key check are all generated from it, so they can't drift apart. A pack can mark more keys protected (`pack.protected_keys`) but can't un-protect an engine key. The Settings tab is `agentflow/config_edit.py`, served by `dashboard.py` (`GET /api/config`, `POST /api/config/preview`/save) and rendered in `dashboard.html`. That means two things. First, a key is either a Form field in `_SECTIONS`/`_catalog()` or listed in `_PROTECTED_KEYS`, never neither. Second, `agentflow validate` and the page's **Review and save** apply the same rules.

| New key | Settings page | Why |
|---|---|---|
| `conductor.enabled`, `conductor.model`, `conductor.effort` | **Form**, new section "Conductor" (bool / text / enum from `effort.allowed`) | Tuning, with no effect on the quality floor |
| `conductor.decision_points` | **Form**, `list` with fixed options (`start, after_plan, on_failure, before_ship`) | Which points it gets |
| `conductor.budget_share`, `conductor.max_replans` | **Form**, number/int, bounded by `policy.limits` (validate refuses a value above it) | The page can tighten them, but never loosen them past policy |
| `autonomy.level.<lane>` | **Form**, section "Autonomy", one `enum` per lane (`L0…L4`, options from `@lanes`) | Promotion is a human's click. Validate refuses a promotion whose scorecard isn't green for that lane, and auto-demotion writes an `autonomy_changed` event, not the file |
| `scorecard.window_runs`, efficiency/compliance alert thresholds | **Form**, section "Scorecard" | Alerts only |
| `catalog`, `policy` (obligations, `forbid_actions_by_conductor`, `limits`), `scorecard.slo` (quality SLOs), `harness_eval` | **Protected**: added to `_PROTECTED_KEYS`, shown read-only and edited in the file through git | They define what "done" means and what fences the agent in, so the dashboard token must not be able to weaken them (the same reasoning as `gates`/`guardrails` today) |

There are also cross-checks in validate, so the Form can't contradict policy:
- `phases.<p>.needs_approval: false` is refused when a policy obligation requires `approval:<p>` (today `define` and `plan`).
- `autonomy.resolve_kinds` can't take a kind that policy marks human-only (this extends today's `budget_exceeded` check).

Read-only views change too:
- The **Workflow tab** shows the catalog, each lane's reference route, and the obligations with their `when` condition.
- The **run drawer** gets Route (proposed vs adopted, decisions with rationale) and Conformance (obligation → evidence seq → tree SHA).
- A new **Scorecard tab** (section 4).

The help text for each new field comes from its line comment in `config.yaml`, as it does today.

### 3.10 Memory and context
Today's rule stays: **no agent session lives for the whole run**. A run lasts hours and a goal run lasts days, so the only things that persist are files and events. Sessions are short and disposable. The harness adds memory *above* the session, in five tiers. Each tier has one writer, a size cap, and a fixed way into a prompt:

| Tier | Holds | Lives in | Writer | Reaches a prompt as | Status |
|---|---|---|---|---|---|
| **1. Working** (one session) | The step's own reasoning | The `claude -p` session | The agent | Itself. Auto-compact at 60% (`context.autocompact_pct`), runner rollover at 80% into a fresh session seeded from `progress.md`, `max_rollovers` → split the task | exists (`agents.py`, `lanes.py:_rollover`) |
| **2. Step** (survives compact, rollover, resume, fix loop) | What's done, files touched, what's next, doubts | `workspaces/<run>/progress.md`; the same session is resumed for fix loops and refine rounds | The agent (`_preamble.md` rule, mtime-checked: `progress_not_updated`) | A path in the prompt | exists |
| **3. Run (episodic)** | Every fact of the run | `runs/<run>/events.jsonl` (the truth), `handoff.md` between phases | **Runner only** (`pipeline.write_handoff`) | Handoff path; gate output trimmed to `gate_output_lines` | exists, plus a new **run digest** |
| **4. Goal** (long-running, multi-run) | Objective, child runs, merged/open/next, why | `runs/<goal>/events.jsonl` + `ledger.md` | **Runner only**: rendered from goal events and each child's PR-body summary | The `ledger.md` path in the conductor prompt | new |
| **5. Project and procedural** (permanent) | *Semantic*: specs, plans, ADRs, idea doc, CLAUDE.md, GLOSSARY. *Procedural*: `lessons.md`. *Statistical*: `stats.json` | The repo; lessons in **two layers**: engine `lessons.md` (model behaviour, true for any project, shipped with engine releases) + pack `.agentflow/lessons.md` (this project); `stats.json` per project under the runs dir | Specs/ADRs: the existing steps (`record_step` for spec gaps). Lessons: **human-merged PR only**. Stats: **runner** (from `metrics.aggregate`) | Paths (grep a section, never read whole); lessons ≤ 4k tokens inline; stats as a compact table | partly new |

**The conductor is stateless by design.** Each decision point (3.3) is a fresh read-only session. It gets a **run digest** that the runner builds deterministically from `events.jsonl` (new `digest.py`, capped at `memory.digest_tokens`, default ~15k):
- the task and lane
- the current route and its progress
- obligations with their evidence status
- budget spent and remaining
- the last N `decision` events, with their rationale (so it remembers *why* it chose before)
- open stops and failed attempts, one line each with a pointer to the event seq
- the `agentflow explain` output

It holds no transcripts and no file contents, only paths. This is what lets a run survive a dead runner, a usage-limit hold, or a 2-day wait for a merge: resume rebuilds the digest from events, so there is no conductor context to lose. It also keeps every decision reproducible, because the inputs come from the event log.

**Learning across runs (procedural memory) is gated, never self-written:**
- **`stats.json`** is runner-computed priors per task kind, module and model: cost, gate first-pass rate, fix attempts, rollovers. The conductor uses them for model, effort and task-split choices. It's numbers from events, so it can't be poisoned by model text.
- **`lessons.md`** holds short rules with evidence. Example: "Sonnet never loads a skill by name, so give a `brief` (runs to 2026-11-02, 0/96)". Each entry carries `source: <run>#<seq>` and `review_by: <date>`. A weekly **retro** step (read-only, Opus) reads the scorecard and *proposes* additions and expiries as a PR. A human merges it. The file is on `guardrails.forbidden_paths` and in `_PROTECTED_KEYS` (via `memory.lessons_path`), and it's conductor input, so a change runs the route-eval suite (4.3) like a prompt change. Bad lessons can't spread silently.

**Context hygiene rules** (enforced by the runner and checked by tests):
- Only runner-written (tiers 3–4) or human-merged (tier 5) text enters a *conductor* prompt. Agent-written `progress.md` only seeds the *same step's* next session.
- Retrieved, CRM, PR and CI-log text is quoted as data, never as instructions (the same rule the product follows).
- Prompts pass paths, not contents. Big docs are grepped by section (existing `_preamble.md` rule).
- Every memory tier has a cap. Going over it truncates oldest-first and records a `memory_truncated` event, so it shows up on the scorecard and is never silent.
- Child runs in a goal never share sessions. They share only the ledger and the repo, so one child's context failure can't spread.

**Measured** on the scorecard (section 4): context % at step end, compactions and rollovers per step, `progress_not_updated` rate (154 today, worth fixing first), digest size, `memory_truncated` count, and lesson hit rate (decisions whose rationale cites a lesson). A lesson that is cited but followed by a KPI regression is flagged for expiry at the next retro.

New config keys `memory.digest_tokens`, `memory.decisions_in_digest`, `memory.lessons_max_tokens` and `memory.retro_every_days` go in the **Form** (section "Memory"), and validate caps them. `memory.lessons_path` and `memory.stats_path` are **protected** (3.9).

### 3.11 Cost governance: bounded spend, bounded time, no silent burn
A model that chooses its own route can also choose to keep trying. So **every dollar is spent against a cap the runner enforces, and every cap ends in a stop for a human, never in "keep going".** This is engine code, so it works the same in every project.

**Today (keep all of it):**
- `run_budget_usd`, a hard stop that waits for an approval
- `task_budget_usd` scaling
- the price table for sessions with no reported cost
- per-loop caps: `fix_attempts`, `resolve_kinds`, `finding_rounds`, `max_rollovers`, `ci.fix_rounds`/`rerun_rounds`
- `step_timeout_min`, `max_turns`
- `recovery.failed_attempts`

Holds (usage limit, CI wait, human wait) already cost no tokens: the runner idles and polls `gh`, not the model.

**What the harness adds:**

1. **A budget hierarchy with hard caps at every level.** Every cap is checked *before* a session starts and *during* it:

   | Level | Cap | Where set | At the cap |
   |---|---|---|---|
   | User / machine | `budget.monthly_usd` across all projects | user layer (`~/.config/agentflow`) | No new session starts anywhere; running sessions finish their turn |
   | Project | `budget.project_monthly_usd` | project pack | The same, for this project |
   | Goal (long-running) | `goal.budget_usd` + `goal.deadline` | `agentflow goal --budget --deadline` | Goal stops; child runs pause |
   | Run | `run_budget_usd` (as today, scaled by plan size) | pack / `--budget` | Stop + stop report |
   | Action | `estimate × budget.action_overrun` (default 2×), with the estimate from `stats.json` p90 | engine | The session is ended after its current turn, like a rollover, and the step stops |
   | Conductor | `conductor.budget_share` (≤ 10% of the run) | pack | Fall back to the reference route; no more decisions |
   | Retries | the loop caps listed above, **all counted in the same run budget** | pack | Stop |

   Costs are summed **per turn** from stream-json, not at session end, so a runaway session is cut mid-step.

2. **Forecast before spending.** Every route the conductor proposes carries a cost estimate, built from `stats.json` (p50/p90 per action kind, model and task size). The validator (3.4) rejects a route whose **p90 forecast is over the remaining budget**. The conductor then has to descope, pick cheaper models for mechanical actions, or ask a human, *before* any money is spent. At each decision point the runner re-forecasts the remaining route: `remaining_forecast = Σ p90(remaining actions)`.

3. **A no-progress circuit breaker** (the "burns tokens and goes nowhere" case). The runner counts **progress events**:
   - `task_committed`
   - a gate going red → green
   - `obligation_satisfied`
   - a blocking finding closed
   - `phase_finished`
   - a child run merged (goal)

   It trips a `no_progress` stop when any of these happens since the last progress event:
   - spend > `budget.no_progress_usd` (default 15% of run budget), or
   - more than `budget.no_progress_sessions` sessions (default 4), or
   - wall time > `budget.no_progress_min` (default 90 min of agent time)

   **`no_progress` is human-only.** The resolver may not take it, because the resolver also spends.

4. **Loop detection.** Each gate failure and each stop gets a **signature**: a hash of the failing gate, the failing test ids and the first error lines. A fix attempt that returns the same signature as the previous one is not retried. The step goes once to a re-plan (conductor, read-only, cheap), and if that signature comes back again it goes to a human. This replaces "use up `fix_attempts` on the same error".

5. **Variance check at milestones.** At each phase end and goal child, the runner compares actual vs forecast spend. If actual > forecast × `budget.variance_stop` (default 1.5×), the run stops with the stop report even though budget remains. Being far off-plan is itself the signal that the run is not converging.

6. **Stop report**, written whenever a run stops on budget, no-progress, variance, deadline or loop. The runner builds it from events, with no model call. It contains:
   - what is done (tasks ticked, obligations met, commits on the branch)
   - what is left
   - why it is stuck (the last failure signatures)
   - the spend breakdown (rework vs useful)
   - the p50/p90 forecast to finish

   It also lists the human's options:
   - **Top up** by a stated amount, with the forecast that it finishes (`agentflow approve <run> --budget <usd>`)
   - **Descope:** mark the remaining plan tasks deferred (already supported, commit 277099b) and ship what is complete. Ship still requires 100% conformance on what is there.
   - **Take over:** `agentflow attach <run>` in its worktree
   - **Cancel**

   Work is never thrown away: commits stay on the run's branch and the worktree is kept. Nothing is pushed until `finish`.

7. **Cheaper by default.** The conductor's model and effort choices are scored on cost (section 4), and cost-saving defaults stay engine defaults:
   - mechanical actions on the small model
   - `gates_first` evidence instead of re-running suites inside a session
   - batching small tasks
   - 5-minute prompt caching
   - stateless conductor with a capped digest (3.10), so no context grows without bound

**Measured** (Scorecard and a **Budget panel** in the run drawer, with a burn-down and the forecast band):
- $ per run, per task and per progress event
- rework % of spend
- forecast accuracy (actual / p90)
- % of runs stopped on budget, no-progress, variance or loop
- top-ups per run
- spend after the last progress event at stop (should trend to $0)

**Settings (3.9):**
- In the **Form**, section "Budget": `budget.*` caps and thresholds, plus `goal.deadline` defaults. Personal caps are written to the user layer.
- `validate` refuses any cap at 0 or unset (no "unlimited"), and a run budget above the project monthly cap.
- `budget.no_progress_*` can be tightened from the Form. Loosening past the engine maximums (for example `no_progress_usd` > 50% of run budget) is refused.

---

## 4. Measuring and tracking

All of it comes from `events.jsonl`, the existing single source of truth, through `metrics.py`. Add a **Scorecard** tab on the dashboard and `agentflow scorecard`.

### 4.1 New events
`route_proposed`, `route_rejected{rule}`, `route_adopted{source: conductor|reference}`, `decision{point, choice, rationale, alternatives}`, `replanned{reason}`, `obligation_computed`, `obligation_satisfied{evidence_seq, tree_sha}`, `obligation_missing`, `conformance_passed|failed`, `autonomy_changed{lane, from, to, reason}`.

### 4.2 KPIs. The baseline is the reference-route runs, frozen before L1
**Quality (must not regress; these are hard SLOs):**
- Conformance rate = **100%** (any miss blocks ship and is a P0 incident)
- Gate first-pass rate per task; fix attempts per task
- Blocking findings at branch/security review (escapes from build)
- CI first-pass rate; CI fix rounds
- Human outcome: `phase_rejected` rate, PR change-requests, post-merge bug runs on the same module within 14 days, reverts
- Diff-cover ≥ 80% (already a gate); eval scores (eval lane)

**Compliance (lower is better; alert on trend):**
- Guard trips per run: `forbidden_path`, `agent_commit`, `rejected_trailer`, `user_only_skill`
- `permission_denied`, `progress_not_updated`, `footer_mismatch` per session
- Route rejection rate, conductor fallback rate, re-plans per run

**Efficiency (where the harness should win):**
- $ per run and per task, rework $, wall time, human-wait time, human interventions per run

**Decision quality:**
- Route agreement with reference (shadow), and "regret": a decision later reversed by a re-plan or a resolver

### 4.3 Harness eval suite (gates changes to the conductor itself)
- **Route eval (cheap, runs in CI):** about 30 frozen task snapshots (task text, `explain` output, diff summary) with expected obligations and acceptable routes. It scores obligation coverage (must be 100%), validity, and cost estimate. It blocks any change to `conductor.md`, the catalog or the policy on a regression, against `.agentflow/harness_eval/baseline.json`. This mirrors the product's eval gate.
- **Replay benchmark (expensive, run on demand):** 5–8 past tasks replayed from their fork SHA in throwaway worktrees, reference route against conductor route, compared on the KPIs. It runs before each autonomy promotion.

---

## 5. Rollout (each slice is one testable PR; feature lane on agentflow itself)

**Slice P (done, 2026-11-05; `docs/plans/2026-11-05-agentflow-harness-slice-p.md`): separate the engine from the pack.**
- Move a project's config, prompt wording and lessons into `.agentflow/`, and keep `agentflow/` as the engine.
- Remove the hard-coding listed in 1b.
- Add the two fixture packs and the denylist test.
- `agentflow explain` output for every lane must be identical before and after.

**Slice PnP (done, 2026-11-06; `docs/plans/2026-11-05-agentflow-harness-slice-pnp-1.md`, `docs/plans/2026-11-06-agentflow-harness-slice-pnp-2.md`): make the engine plug and play** (1c; its deviations are recorded at the end of 1c):
- packaging as an installable tool with a console script
- the state dir outside the repo
- layered config and `config show --effective`
- `init` (detect, starter, sparse pack, gate baseline) and `doctor`
- the bundled generic skills plus `skills.map`

Every later slice then lands in the engine already generic. The engine already lives in its own repository (this one, with its own `.agentflow/` pack); what remains of the cutover is installing it as a tool. Playbook §13 "Using agentflow in another repository" is replaced by `agentflow init --starter <python-uv|node-pnpm|docs-only>`.

**Slice TB-A1 (done, 2026-11-07; `docs/plans/2026-11-07-agentflow-harness-slice-tb-a1.md`): the team backlog from spec files** (1d; its deviations are recorded there):
- the `specs` work source, with dependencies read from each spec's header
- the Backlog starts spec modules and new feature cards, and names the files that are not modules
- `init` writes the `specs` source and a feature-starting Backlog when it finds a specs directory
- this repository's pack turns the source on

**Slice TB-A2 (done, 2026-11-07; `docs/plans/2026-11-07-agentflow-harness-slice-tb-a2.md`): finished modules in the team backlog** (1d; its deviations are recorded there):
- finished work is read from `origin/<fork_from>`: a spec that says done, or a complete plan that names its spec, before `gh` is asked about a merge
- each done module shows its reason in the Backlog
- `agentflow backlog done` and `undone` mark a module by hand or veto origin evidence, kept in `runs/done_marks.json`
- the module list and the fresh re-check before a start move to PR B

**Rule for every slice:** any `config.yaml` key it adds or changes ships with the matching Settings change (3.9): a Form field or a `_PROTECTED_KEYS` entry, validate cross-checks, `tests/test_config_edit.py` cases, and the field list in guide §11 "Changing behaviour".

0. **Baseline and scorecard.** Add `tree_sha` to gate/review events, KPIs in `metrics.py`, the `agentflow scorecard` CLI and dashboard tab. No behaviour change.
1. **Policy and conformance audit** (G3, G5), running on the *current fixed workflow*. It should pass at 100% straight away, which proves the obligations match today's bar. Switch it from report-only to blocking.
2. **Catalog and reference routes** (G1). Re-express the lanes as routes. A pure refactor: the existing `agentflow/tests/` must pass unchanged.
3. **Route validator, run digest and conductor in shadow mode** (G2, G4, L0, 3.10 tier 3). Log routes and agreement, build the route-eval suite, and start writing `stats.json`.
3a. **Cost governance** (3.11): the budget hierarchy, per-turn metering, the action cap, the no-progress breaker, failure signatures, the stop report and the Budget panel. This lands **before** L1 (step 4), so no conductor decision ever runs without these caps.
3b. **Lessons and retro** (3.10 tier 5): the `lessons.md` seed (written by hand from the existing config comments) and the weekly retro step that proposes PRs.
4. **L1 on `bug` and `eval` lanes**, then `feature`. Compare against the baseline.
5. **L2/L3:** optional actions, spikes, task splits, mid-run re-plans.
6. **L4 goal runs** (G7) on top of `module_queue.py`.

### Critical files
(Engine files are in `agentflow/`, and pack files in `.agentflow/`.)
- New: `agentflow/{route.py, policy.py, conductor.py, conformance.py, scorecard.py, digest.py, goal.py}`, `prompts/conductor.md`, `prompts/retro.md`, `lessons.md`, `harness_eval/`
- Change: `config.yaml` (`catalog`, `policy`, `conductor`, `autonomy.level`), `config.py` (schema plus validate), `pipeline.py` (`_Loop`: next action from the route; decision points), `events.py`/`state.py` (new event types), `gitops.py` (conformance before `finish`), `metrics.py`, `dashboard.py`/`views.py`/`dashboard.html` (Route and Scorecard views), `config_edit.py` (new Form sections "Conductor" and "Scorecard", per-lane `autonomy.level` fields, and the new `_PROTECTED_KEYS` entries; see 3.9), `tests/test_config_edit.py`
- Reuse: `conditions.evaluate`/`explain`, `guards.check_step`/`snapshot`, `autonomy.Resolver`, `module_queue.py`, `metrics.run_metrics`/`aggregate`
- Docs: new `docs/agentflow-harness-proposal.md` (this proposal), then `docs/agentflow-playbook.md` and the CLAUDE.md engine sections once each slice ships

## Next step
Each slice is planned with `writing-plans` (`docs/plans/YYYY-MM-DD-agentflow-harness-<slice>.md`), starting with slice P (engine/pack separation).

## Verification
- Slice 1: run `agentflow run feature --module <m> --dry-run` and a real past-shaped run. The conformance audit must report 100% on the reference route. Then deliberately skip `branch_review` in a test config and check that the audit blocks `finish` and queues the review.
- Slice 2: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q` is green with no test edits, and `agentflow explain` output is identical before and after for every lane.
- Settings (every slice): `agentflow/tests/test_config_edit.py` covers three cases. Each new Form field round-trips through `propose`/`save` and keeps comments and anchors. Each new protected key is refused from the page with the "protected" message. A `needs_approval: false` on `plan`, or an autonomy promotion with a red scorecard, is refused by validate. Then a manual check: `agentflow dashboard` → Settings shows the new sections with help text, and Review and save shows the diff.
- Plug and play (engine CI, every release): an e2e test clones three fixture repos (Python/uv with a GitHub Actions workflow, Node/pnpm, docs-only with no skills dir). It runs `init` → `doctor` → `validate` (0 warnings after `gates_confirmed`; see §1c, deviation 4) → `explain` → `run --dry-run`. Pass criteria:
  - the gates are detected from CI
  - nothing is written in the repo except `.agentflow/`
  - `rm -rf .agentflow` leaves `git status` clean
  - time-to-first-dry-run is under 10 minutes
- Goal grounding:
  - A Goal Brief fixture with one fabricated citation (a missing heading) and one uncited criterion must be refused as not ready, with exactly those two items listed as questions.
  - Editing a cited spec section after approval must raise `goal_source_changed` and pause the dependent child.
  - A child diff outside goal scope must raise `goal_scope_drift` and block its ship.
  - `approval:goal` can't be disabled from the pack, the Settings page or any autonomy level.
- Cost governance: run a fake-agent run (the existing `tests/fake_agent.py`) scripted to do four things:
  - burn cost with no progress → it must stop `no_progress` at the threshold, and the resolver must not be invoked
  - repeat the same gate failure → it must stop after one re-plan, not after `fix_attempts`
  - exceed an action cap mid-session → the session must be cut after the current turn
  - propose a route whose p90 is over the remaining budget → the validator must reject it

  Each stop writes a stop report with a forecast, and no cap can be set to 0/unset through `validate` or the Settings page.
- Portability (every slice): the engine suite passes on both fixture packs, the denylist test passes, and `agentflow init --starter python-uv` in an empty temp repo followed by `agentflow validate` prints 0 warnings (see §1c, deviation 4).
- Memory: kill the runner mid-build, then `agentflow resume`. The rebuilt digest must equal the one from before the kill (byte-compare, from events only). A test checks that a conductor prompt contains no agent-written text (no `progress.md` content) and stays under `memory.digest_tokens`. An over-cap digest emits `memory_truncated`.
- Slice 3+: the route-eval suite scores 100% obligation coverage, and the scorecard shows conductor runs against the baseline with no SLO in red before any promotion.
