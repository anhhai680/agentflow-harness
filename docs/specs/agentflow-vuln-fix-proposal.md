# Proposal: a `vuln` lane that patches vulnerable dependencies, from a Dependabot alert to a reviewed PR

Status: approved (2026-11-23)

Builds on the bug lane (`bug_eval.py`'s red/green expectations), intake (`intake.py`), the policy and conformance
audit (`docs/specs/agentflow-conformance-proposal.md`) and the Backlog issue sync
(`docs/specs/agentflow-issue-sync-proposal.md`). The security *overlay* (`overlays.py`) is a different thing: it adds
care to a run that touches security-sensitive code. This proposal adds a *lane* for one kind of work, which is
patching a known vulnerability in a dependency.

## Objective

agentflow has no way to fix a security alert today.

1. **No input for an alert.** A run starts from `--module`, `--topic`, `--issue`, `--log` or `--task`. A Dependabot
   alert, or a GHSA/CVE id from `pip-audit`, `pnpm audit` or `govulncheck`, has to be retyped as `--task` text.
   The run then has none of the alert's facts: the package, the ecosystem, the manifest, the vulnerable range and
   the first patched version.
2. **The bug lane is the wrong shape.** It runs `debug` and then `regression_test` with `expect: red`. For a
   dependency bump, a failing test that reproduces the CVE usually can't be written, so the run stops at
   `red_test_passes` or `blocked`, or the agent writes a fake test to get past it.
3. **Nothing proves the vulnerability is gone.** Gates prove that the tests pass, not that the vulnerable version
   left the lockfile. A run can upgrade the wrong package, or "fix" the alert by adding it to an audit tool's
   ignore list, and still finish green and conformant.
4. **Dependabot's own PRs are not enough.** They bump one version. They don't fix the code an upgrade breaks, they
   don't resolve a transitive dependency that needs an override, and many of them sit red.

**Who it's for:** a maintainer who has a queue of open Dependabot alerts and wants each one to become a PR that is
ready for review, with the same guarantees a bug run gives: verify, review, CI, and no merge.

### Acceptance criteria

- `agentflow run vuln --alert 42` reads Dependabot alert 42 through `gh`. It refuses an alert that is closed, has no
  patched version, or is below `vuln.min_severity`, and starts no run when it refuses. Otherwise it starts a run on
  `flow/vuln/<package>-<ghsa>`.
- Before any agent edits anything, the runner runs the pack's audit probe on the fork commit. The probe's output must
  name one of the advisory's ids (`advisory_present`). If it doesn't, the run stops with `advisory_not_reproduced`
  and no patch session starts.
- After the patch, the same probe must run cleanly and name none of the ids (`advisory_cleared`). Until it does, the
  patch step's fix loop treats the probe as a failing gate.
- No step of the lane may change an audit tool's ignore or config file (`vuln.forbid`). A change to one is a
  `guard_violation`.
- `finish` audits a new `advisory_cleared` obligation, re-running the probe on the final tree when the evidence is
  stale. In `block` mode a run without it doesn't push.
- The PR names the advisory ids, the severity, the package with its old and new version, and the alert's URL. The
  run never dismisses an alert and never merges.
- With `vuln.sync: true`, open alerts at or above `vuln.min_severity` appear as Backlog cards, one per package and
  manifest (slice V2).

## Decided behaviour

- **Lane name: `vuln`.** The name `security` is taken by the overlay.
- **One run per package and manifest.** `--alert N` also pulls in the other open alerts for the same
  `(ecosystem, package, manifest_path)`, so one upgrade closes all of them. The run's advisory ids are the union of
  those alerts' ids.
- **The audit tool is the judge, not the engine.** The engine parses no lockfile. The pack names an audit gate
  (`vuln.probe`), and the runner judges it by whether its output names the advisory ids. That works for every
  ecosystem whose scanner prints GHSA or CVE ids, and it keeps the engine stdlib-only and project-agnostic.
- **A human approves the upgrade path, as in the bug lane.** `define` runs a `triage` session that writes
  `runs/{run}/triage.md`: direct or transitive, the target version, the bump size (patch, minor or major), and the
  method (bump, override or resolution, or replacing the package). `define` already needs approval, so the human sees
  the triage before anything changes. `autonomy.approvals.define` can ramp it to `auto` like any other phase. The
  model can always add a stop (`needs_human` for "no safe upgrade path"), but it can never remove one.
- **The PR is the last approval.** The run ends at a PR that is ready for review, as other lanes do. Dependabot
  closes the alert by itself once the fix reaches the default branch.
- **Triage approval always asks by default.** `autonomy.approvals` is per phase, so the `vuln` lane gets no default
  of its own. A pack that wants the ramp turns on `autonomy.approvals.define` itself. A per-lane ramp key is out of
  scope.
- **No release cooldown in the engine.** The engine never asks a registry for publish dates. A cooldown comes from
  the package manager's own setting in the repository (pnpm, yarn and uv have one; Go has none), and the patch
  session must respect it. `vuln_review.md` raises a `high` finding, which blocks the run and starts a refine round,
  when the target version was published less than 7 days before the run. Engine enforcement may come back in V3.
- **A Dependabot PR already open for the package: warn and go on.** Intake prints a warning and records the PR
  number. The agentflow PR says it supersedes it, and the V2 card shows "Dependabot PR #N open". agentflow never
  comments on, closes or pushes to a Dependabot PR; a human does that. Taking over a red Dependabot PR (`--pr N`)
  is a V3 candidate, not a commitment.
- **One run covers one package in one manifest**, with all of that package's open alerts. Low-severity alerts are
  never batched across packages.
- **`advisory_cleared` is on the engine floor for the `vuln` lane.** A pack whose ecosystem has no scanner can't use
  the lane. It patches by hand, or with `bug`/`--task`.

## Design

### 1. Intake: `--alert N`

`intake.gather` gets a fourth source, next to `--issue`, `--log` and `--task`:

- `--alert N` must match `ISSUE_REF` (digits only). The runner calls
  `gh api repos/{owner}/{repo}/dependabot/alerts/N` as an argv list with no shell. It then lists
  `dependabot/alerts?state=open&package=<name>&ecosystem=<eco>&per_page=100` to find the sibling alerts and keeps
  those with the same `manifest_path`.
- Every field the runner *uses* is validated before use, and a field that fails stops intake with a message:
  - ids: `^GHSA(-[23456789cfghjmpqrvwx]{4}){3}$` and `^CVE-\d{4}-\d{4,7}$`;
  - ecosystem: one of the values GitHub documents;
  - package name: printable, with no whitespace and no shell metacharacters, at most 214 characters;
  - manifest path: relative, with no `..`, and a file that exists at the fork commit (`gitops.read_blob`);
  - versions: at most 64 characters, from `[0-9A-Za-z.+~_-]`.
- `alert.json` keeps those validated facts, and the runner reads only that file. `alert.md` keeps the advisory's
  summary, description and references for the agent, under `intake.DATA_NOTE`. The advisory text comes from outside
  the run. Prompts point at the file, as they do for `issue.md`, and none of it ever reaches a shell command or a
  gate.
- A refusal leaves no run behind, as with a bad `--issue`. The refusal cases are an empty `vuln.probe`, a `state` other than `open`, a null
  `first_patched_version` (the message lists what a human can do: wait, replace the package, or dismiss the alert in
  GitHub), a severity below `vuln.min_severity`, or `vuln.source: ""`.
- The slug is `<package>-<first ghsa id>`, made with `slugify`.
- **Open Dependabot PRs.** Intake calls
  `gh pr list --author app/dependabot --state open --search <package> --json number,title,url --limit 20` (an argv
  list; the package name is already validated). It keeps the PRs whose title names the package as a whole token.
  Their numbers go into `alert.json` as `dependabot_prs`, and the CLI prints `warning: Dependabot PR #N is open for
  <package>; this run's PR will supersede it`. A `gh` failure here only drops the warning, never the run.

### 2. The lane's steps (engine defaults, `lanes: [feature, bug, vuln]`)

| Phase | Step | What it does |
|---|---|---|
| define | `triage` (new, role `plan`, prompt `vuln_triage.md`, skill `@skills.map.patch`) | reads `alert.md` and the manifest, writes `runs/{run}/triage.md`; the phase's approval follows |
| build | `setup` | existing gate step; `vuln` is added to its `lanes` |
| build | `advisory_red` (new, `type: gate`, `gates: []`, `expect: advisory_present`) | the runner runs the `vuln.probe` gate on the fork commit; no agent |
| build | `patch` (new, role `build`, prompt `vuln_patch.md`, `gates: build`, `expect: advisory_cleared`, `fix_loop`, `commit: true`, message `Patch {slug}`) | upgrades with the ecosystem's own package manager, regenerates the lockfile and fixes what breaks |
| verify | `setup`, `verify` | existing steps; `vuln` is added to their `lanes` |
| review | `plan_fixes`, `fix_findings` | existing steps; `vuln` is added to their `lanes` |
| review | `vuln_review` (new, role `review`, prompt `vuln_review.md`, `blocks_on_findings`) | the supply-chain review, in place of `branch_review` for this lane |
| ship | `finish`, `ci` | unchanged; they have no `lanes` tag |

`skills.map.patch` defaults to a new bundled skill, `agentflow-patch`. It holds the method: the smallest upgrade that
clears every advisory id, using the package manager and not hand-edited lockfiles (`uv lock --upgrade-package`,
`pnpm update`, `go get pkg@vX`, and overrides or resolutions only for a transitive dependency); no unrelated
upgrades; fix the code that breaks; never touch ignore lists. `vuln_review.md` asks the reviewer to check:

- the diff upgrades only what the advisories need;
- new transitive dependencies, and any maintainer or source change, are named;
- the version that is pulled in really is at or above `first_patched_version` in the lockfile;
- a major bump's breaking changes are covered by the code changes or the tests;
- the target version's publish date. The patch session records it in its result, as data. A version published less
  than 7 days before the run is a `high` finding, and so is ignoring the repository's own release-age setting.

A pack that writes its own `phases:` doesn't get the lane. It adds `vuln` to its `lanes:` and copies these steps
(playbook §14).

### 3. The probe: `expect: advisory_present` and `expect: advisory_cleared`

`bug_eval.py`'s expectation machinery gains two values (`config.EXPECT_VALUES`). Both read the run's advisory ids from
`alert.json` and run the gate named by `vuln.probe` in the worktree, as an ordinary gate (the pack's own shell
command, `gate_timeout_min`, a log under the step's gate directory). Neither one runs anything from the agent or
from the alert.

- An id **matches** when it appears in the gate's combined output as a whole token (`(?<![\w-])ID(?![\w-])`).
- A probe **ran cleanly** when it didn't time out, its output isn't empty, and one of these holds:
  - its exit code is in `gates.<probe>.ok_exit` (default `[0]`);
  - its exit code is in `gates.<probe>.found_exit` (default `[1]`) and its output names at least one advisory id of
    any kind (`GHSA-…`, `CVE-…`, `GO-…`, `PYSEC-…`, `RUSTSEC-…`).

  Anything else is `probe_failed`, which stops the run and is never counted as green. That covers `127` for a
  missing tool, a crash, a timeout, and a "found" exit that names no advisory. This closes the hole where a scanner
  that dies looks clean: `pip-audit` exits 1 both when it finds something and when it crashes, and only the first
  case names an id.
- `advisory_present` passes when the probe ran cleanly and at least one id matches. If no id matches, the stop is
  `advisory_not_reproduced`: the alert may be stale, or the scanner may not see it. The human can cancel the run, fix
  the probe, or `retry`.
- `advisory_cleared` passes when the probe ran cleanly and no id matches. Other advisories in the output don't count,
  since the run fixes its own. Its output is attached to the handoff so the reviewer sees what is left.
- Each check emits `expectation_checked` with `expect` and the matched ids, like `red` and `green` do today.

The go starter's probe uses `govulncheck -format json`, whose OSV entries carry GHSA and CVE aliases. Its text
output names only `GO-` ids.
`pip-audit` likewise needs `--aliases on` to print GHSA and CVE ids next to its `PYSEC-` ids. `detect` adds a note
when it proposes a `pip-audit` step from CI that lacks it.

### 4. Guards: an ignore list is a moved baseline

In a `vuln` run, every agent step, every check the runner makes after a command, and `ci`'s fix session forbid
the engine's fixed list `vuln.ENGINE_FORBID` plus the pack's `vuln.forbid` (default `[]`). The runner adds the two
together in code, so a pack can add entries but can never drop the engine's. The engine's list names the files
that silence a scanner:

```python
ENGINE_FORBID = (".github/dependabot.yml", ".github/workflows/**", "**/.nsprc", "**/audit-ci.json*", "**/.snyk",
                 "**/osv-scanner.toml", "**/.trivyignore", "**/.grype.yaml", "**/pip-audit*.toml",
                 "**/.cargo/audit.toml")
```

An edit to one fails the step as `guard_violation`, as an edit to the eval lane's `baseline.json` does. For
ignore flags inside a shared file (`[tool.pip-audit]` in `pyproject.toml`, or `auditConfig` in `package.json`),
`vuln_review.md` checks the diff, and the probe command is the pack's own text, which the agent can't change. The
lane never calls the alert API to write: intake uses `GET` only.

### 5. Policy and conformance

```yaml
policy:
  obligations:
    verified:         {lanes: [feature, bug, vuln], require: ["step:verify"]}
    vuln_reviewed:    {lanes: [vuln], require: ["step:vuln_review"]}
```

The engine adds `advisory_cleared` in code, as it adds `engine_floor`:
`Obligation("advisory_cleared", (Evidence("expect", "advisory_cleared"),), lanes=("vuln",))`. No layer can null
it or change it, and `validate` warns about an entry that tries. `config.EXPECT_EVIDENCE` gains `advisory_cleared`.
The evidence is an `advisory_cleared` check that passed, comes after an `advisory_present` check that passed, and
isn't stale. In `conformance.py`, the evidence goes stale under the same
`_staling` rules as `red_then_green`. When it is stale, `finish` runs the probe once on the tree it is about to push,
as it does for a gate with no passing result. `advisory_cleared` joins the engine floor for the `vuln` lane: a pack
can't switch it off, because a vuln run that doesn't prove the fix has no point.

### 6. The PR and the handoff

`finish` adds a `Security advisory` section to the PR body. It lists the ids, the severity, the package, the
manifest, the version range, and the from and to versions from `triage.md` (as data, quoted), plus the alert URL.
For each of `dependabot_prs`, it adds a line `Supersedes #N (Dependabot)`, and it never touches that PR.
The section also shows the probe's before and after checks (`Probe: present (<ids>) → cleared`). No phase after
`define` waits for approval in this lane, so the PR is where a person sees them. A
`Code changed outside dependency files: <paths>` line appears when the diff touches files outside
`vuln.dependency_files` (deterministic, from `changed_paths`), so the reviewer knows the upgrade needed code changes.

### 7. Starters, detect, init and doctor

- **Starters** add a pinned `audit` gate and `vuln.probe: audit`. It is in no gate set, so it never slows another
  lane:
  - `python-uv`: `uv run --frozen --with pip-audit==2.9.0 pip-audit --progress-spinner off --aliases on`, with
    `ok_exit: [0]` and `found_exit: [1]`. Without `--aliases on`, `pip-audit` prints only `PYSEC-` ids, never the
    alert's GHSA or CVE id, so the probe could never reproduce the advisory (checked against `requests==2.31.0`);
  - `node-pnpm`: `pnpm audit`, with `ok_exit: [0]` and `found_exit: [1]`;
  - `go`: `go run golang.org/x/vuln/cmd/govulncheck@v1.1.4 -format json ./...`, with `ok_exit: [0]` and
    `found_exit: []`, since its JSON mode exits 0 either way;
  - `docs-only`: none, with `vuln.source: ""`.
- **detect** recognises `pip-audit`, `npm audit`, `pnpm audit`, `yarn audit`, `osv-scanner`, `govulncheck`,
  `cargo audit`, `bundle audit` and `trivy fs` in CI steps and proposes them as the `audit` gate.
- **doctor**: when `vuln.source: dependabot`, it calls `gh api repos/{owner}/{repo}/dependabot/alerts?per_page=1`
  (`GET`, read-only). A `403` or `404` reports the missing permission ("Dependabot alerts: read") or that alerts are
  off, as a warning (intake refuses such a token itself, naming the permission). When the probe gate's program is missing from `PATH`, it says so. This widens doctor's "only command it runs"
  rule by one read-only `gh` call, and the playbook says so.

### 8. Backlog sync (slice V2)

`backlog.sync_alerts` runs next to `sync_issues`, once per refresh. It makes one `gh api` call with
`state=open&severity=<min..critical>&per_page=100`, groups the alerts by `(ecosystem, package, manifest_path)`, and
caches them in `backlog_state.json` with the same failure note on a `gh` error. A card shows the package, the highest
severity, and the count of alerts. Start runs `vuln --alert <lowest number in the group>`. A group with a live run is
"in progress". A group whose alerts all lack a patched version shows "no fix yet", and Start is refused (409).

### 9. Signals

`signals.py` gains `advisory_reproduced` (0/1), `vuln_code_change` (0/1) and `hours_to_pr:<severity>`. A lesson can
target them, and the scorecard shows them in the Metrics tab.

### 10. V3: beyond Dependabot

Status of this section: approved (2026-11-24). It adds two CLI inputs and one config key that §1 to §9 don't have.
V3 builds the two committed items of §Rollout V3. The two candidates (`--pr N`, an engine release cooldown) stay
unplanned.

**`--advisory ID --package NAME --manifest PATH`.** This starts a vuln run in a repository without Dependabot alerts.
- `ID` must match `GHSA_ID` or `CVE_ID`. The runner reads GitHub's global advisory database with one `GET`, as an argv
  list: `gh api advisories/<GHSA>`, or `gh api advisories?cve_id=<CVE>&per_page=100` for a CVE. That endpoint needs no
  token permission.
- The run keeps the advisories that aren't withdrawn and whose `vulnerabilities[]` name `--package`. It compares
  names by ecosystem: PEP 503 normalisation for `pip`, case-insensitive for `nuget` and `composer`, exact otherwise.
  The run's ids are the union of those advisories' GHSA and CVE ids.
- Each field the runner uses passes the same validators as an alert's. `--package` must pass the package check, and
  `--manifest` the manifest check, as a file at the fork commit.
- Intake refuses a run, and leaves none behind, when:
  - no advisory names the package;
  - the matches span more than one ecosystem;
  - every match is withdrawn;
  - no match has a `first_patched_version`;
  - the highest severity is `unknown` or below `vuln.min_severity`.
  A match with no patched version, alongside one that has one, is dropped with a warning.
- `vuln.source` doesn't apply: it governs Dependabot intake only. `--alert` and `--advisory` exclude each other.
  `--package` and `--manifest` are valid only with `--advisory`, and both are required with it.
- `alert.json` keeps the shape `--alert` writes, with `"source": "advisory"`, `"alert": null` and `"alerts": []`.
  Each advisory entry has `"number": null`, and its `url` is the advisory's `html_url`. So the probe, conformance,
  signals and Backlog holds work unchanged. `alert.md` holds the advisory's text under `DATA_NOTE`. The PR's
  Security advisory section says `Advisory: <url>` where an alert run says `Alert: <url>`.
- The Dependabot PR lookup runs as for an alert. The Backlog never starts an `--advisory` run.

**The dependency review check (`vuln.dependency_review: false`).**
- When the check is on, in a vuln run, `ci` makes one call each time CI passes on a pushed sha, before it marks the PR
  ready:
  `gh api repos/{owner}/{repo}/dependency-graph/compare/<base sha>...<pushed sha>`. The base sha is
  `refs/remotes/origin/<fork_from>`, and GitHub resolves the merge base.
- It counts each `added` change whose vulnerabilities reach `vuln.min_severity`. The API's severity isn't a
  documented enum: `moderate` reads as `medium`, and an unknown value counts as a hit, so the check fails closed.
- A hit stops the run as `new_advisory`, which names the ids and packages.
- These stop the run as `dependency_review_failed`: an API error (a `403` means a private repository without
  GitHub Advanced Security, or a fork), a malformed reply, or a reply with no change to the run's package. The last
  case means the dependency graph hasn't caught up, and an empty compare must never read as clean.
- `approve` on either stop accepts that sha. The PR is marked ready, and its section says
  `Dependency review: accepted by <who> (<ids>)`. A later push is checked again. A clean check shows
  `Dependency review: no new advisory at or above <floor>`.
- The check covers the vuln lane only.
- `doctor`, when the key is on, makes one read-only call comparing `<fork_from>...<fork_from>`. It warns on a `403`
  or a `404`.
- The key is protected, because turning it off from the dashboard weakens a run. `validate` wants a bool, and with
  `true` it wants `vuln` in `lanes`.

## Config (each key ships with its four obligations: Settings field or `_PROTECTED_KEYS`, `validate`, a `test_config_edit` case, a playbook §14 line)

```yaml
vuln:
  source: dependabot          # dependabot | "" (no alert intake; the lane refuses --alert)
  min_severity: low           # low | medium | high | critical: lower alerts are refused at intake and not synced
  probe: ""                   # the gate whose output must name the advisory before and not after; "" (the engine default) makes `run vuln` refuse to start; the starters set `audit`
  sync: false                 # true: open alerts become Backlog cards (V2)
  dependency_review: false    # true: after CI passes, a new advisory at or above min_severity stops the run (V3, §10)
  dependency_files: ["**/pyproject.toml", "**/uv.lock", "**/requirements*.txt", "**/package.json",
                     "**/pnpm-lock.yaml", "**/package-lock.json", "**/yarn.lock", "**/go.mod", "**/go.sum",
                     "**/Cargo.toml", "**/Cargo.lock", "**/Gemfile", "**/Gemfile.lock"]
  forbid: []                  # §4: added to the engine's fixed `vuln.ENGINE_FORBID`, never in place of it
gates:
  <name>:
    ok_exit: [0]              # exit codes of a clean scan that found nothing (probe gates only)
    found_exit: [1]           # exit codes of a clean scan that found something
skills:
  map:
    patch: agentflow-patch
```

`validate` checks the following:
- a non-empty `vuln.probe` names a defined gate;
- `ok_exit` is non-empty, `ok_exit` and `found_exit` are disjoint, and both hold ints from 0 to 255 other than 126
  and 127;
- `min_severity` is one of the four values;
- `source` is `dependabot` or `""`;
- `forbid` is a list of globs;
- a pack that lists `vuln` in `lanes` but writes its own `phases:` has an `advisory_red` and a `patch` step tagged
  `vuln`.

`vuln.source`, `vuln.forbid`, `vuln.probe` and `vuln.dependency_review` (V3) go in `_PROTECTED_KEYS`. The other keys get Settings fields.

## Boundaries

- **Always:**
  - treat alert text as data;
  - validate every alert field before use;
  - run the probe before and after the patch;
  - prefer the smallest upgrade that clears every id;
  - end at a PR that is ready for review.
- **Ask first (a stop for a human):**
  - there is no upgrade path inside the manifest's constraints;
  - the fix is a major bump (the triage approval covers this);
  - the fix replaces the package;
  - the probe can't reproduce the advisory.
- **Never:**
  - dismiss or change an alert;
  - edit an ignore list, a scanner config, `dependabot.yml` or a CI workflow;
  - hand-edit a lockfile;
  - put alert text in a shell command;
  - merge;
  - add a third-party import to the engine.

## Testing

- `tests/bin/gh` serves `api repos/{owner}/{repo}/dependabot/alerts[/N]` from a fixture JSON. Cases: open, closed,
  no patched version, a sibling alert, a hostile package name, a `../` manifest path, `403`.
- The end-to-end tests use the `flow_repo` fixture. A test edit adds the engine's own `vuln` steps (read from
  `defaults/config.yaml`) to the python pack's `phases:`. The tests write a toy `deps.txt` and a fake `audit.py`
  probe, which prints a GHSA id while `deps.txt` is under the patched version, exits 1 when it found one, and exits 0
  when it found none. They run with `fake_agent.py`:
  - the happy path: red, then patch, then cleared, then conformant at finish;
  - `advisory_not_reproduced`;
  - `probe_failed` (exit 127, empty output, timeout);
  - a patch that edits `.nsprc` (`guard_violation`);
  - a patch that bumps the wrong package (the fix loop runs out, then a stop);
  - stale `advisory_cleared` re-run at finish.
- `test_intake.py`: field validation, sibling grouping, refusals that leave no run, and the Dependabot PR warning
  (found, not found, a `gh` failure that drops only the warning, a title that names the package only as a substring).
- `test_config_edit.py`, `test_pack.py` (`validate` cases), `test_conformance.py` (the new evidence),
  `test_plug_and_play.py` (detect proposes an `audit` gate from the python fixture's CI).
- `test_portability.py` stays green: the defaults name scanners and file globs, never a project.
- The golden `fixtures/explain/*.txt` stay byte-identical. The python fixture pack writes its own `lanes:` and
  `phases:`, so the new default steps don't reach it. `explain vuln` is pinned in a new test on the `sparse_repo`
  fixture (engine defaults), not in a golden file.

## Files

New:
- `agentflow/vuln.py`: alert fetch, validation and grouping, the probe matcher;
- `agentflow/prompts/vuln_triage.md`, `vuln_patch.md` and `vuln_review.md`;
- `agentflow/skills/agentflow-patch/SKILL.md`;
- `agentflow/tests/test_vuln.py`, `agentflow/tests/test_vuln_lane.py`.

Changed:
- `intake.py` (`--alert`), `flow.py` (`PARAM_FLAGS`, `explain --alert`);
- `config.py` (the `vuln:` block, gate `ok_exit`/`found_exit`, `EXPECT_VALUES`, `EXPECT_EVIDENCE`, validate);
- `defaults/config.yaml`;
- `bug_eval.py` (the two expectations);
- `conformance.py`, `policy.py` (the floor);
- `guards.py` (`vuln.forbid` per step);
- `finish` (the PR section);
- `signals.py`;
- `backlog.py`, `dashboard.html` (V2);
- `detect.py`, `init.py`, `doctor.py`, the starters, `config_edit.py`;
- playbook §10, §13 and §14, `CHANGELOG.md`.

`guards.py`, `detect.py` and `init.py` are security-overlay paths, so plan and review the work with
`security-and-hardening` and the `security-auditor` agent.

## Commands

```bash
agentflow run vuln --alert 42 [--dry-run]
agentflow explain vuln --alert 42
agentflow doctor                       # shows the Dependabot alerts permission and the probe's program
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_vuln.py --import-mode=prepend -q
uvx ruff@0.17.4 check .
```

## Rollout

- **V1, the lane.** `--alert`, the `vuln` lane, the probe expectations, `vuln.forbid`, the obligation, the PR
  section, starters and doctor. CLI only. One PR and a minor bump: adding a lane to the defaults breaks no pack.
- **V2, the Backlog.** Alert sync and cards, grouping, the "no fix yet" state, and the dashboard's severity pill.
- **V3, beyond Dependabot.**
  - Shipped in 3.8: `--advisory GHSA-…|CVE-…` with `--package` and `--manifest`, for repositories without
    Dependabot: the probe alone carries the proof.
  - Shipped in 3.8: after the push, an optional check (`vuln.dependency_review`) against GitHub's dependency review
    API (`dependency-graph/compare/base...head`), so the PR introduces no new advisory at or above `min_severity`.
  - A candidate, not a commitment: adopting a red Dependabot PR with `--pr N`.
  - A candidate: an engine-enforced release cooldown, if the package-manager setting and the review prove too weak.

## Success criteria

- A Dependabot alert on the `flow_repo` vuln setup becomes a conformant run and a PR with no human input beyond the
  triage approval, and `advisory_cleared` comes from the probe, not from the model.
- Each listed bypass is caught by a test: a crashed scanner, an ignore-file edit, a wrong-package upgrade and a stale
  green.
- `validate` prints `0 warnings` on this repository's pack and on both fixture packs, the golden files are
  unchanged, and ruff and the full suite pass.

## Alternatives considered

- **Use the bug lane with `--task`.** The debug and red-test steps don't fit, and nothing proves the fix. Rejected.
- **Parse lockfiles in the engine.** That means a parser per ecosystem (uv.lock, pnpm-lock.yaml, go.sum and more), a
  large untrusted-input surface, and a fact that the scanner already knows. Rejected. The probe asks the scanner.
- **Trust Dependabot's PRs and only fix their CI.** Useful, but it covers only the alerts Dependabot can open a PR
  for. Kept as a V3 candidate.
- **An engine-enforced release cooldown.** It needs registry calls and a publish-date parser for each ecosystem.
  Rejected for V1 in favour of the package manager's own setting and the review finding.
- **Close alerts through the API when the run ends.** That is a write to a security record before a human merges.
  Rejected. Dependabot closes the alert itself on merge.

## Open Questions

None.

**Resolved (grilling, 2026-11-23)**
- Release cooldown: no engine key. The package manager's own release-age setting applies, and the reviewer raises
  a `high` finding for a target version under 7 days old. Engine enforcement is a V3 candidate.
- Dependabot PRs: warn at intake and go on. The PR says it supersedes them, and agentflow never touches them.
  Adopting one (`--pr N`) is a V3 candidate.
- Grouping: one package in one manifest per run, with all of its open alerts. No batching.
- `advisory_cleared` is on the engine floor for the `vuln` lane.
- Triage approval: it always asks by default. A pack turns on `autonomy.approvals.define` itself, and there is no
  per-lane ramp key.
