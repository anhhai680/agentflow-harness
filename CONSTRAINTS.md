# Constraints

Last reviewed: 2026-10-19 by @anhhai680

What "good enough to ship" means in this repo, with the command that decides each rule. Agents read this before writing code and never weaken it to make a change pass. Tightening a rule is a normal commit. Loosening one is its own commit, with the reason and a human's approval (see Overrides).

**Status:** written before the first code root exists. The floor is enforced from today. Every other row names its command, which starts running when the platform skeleton lands (SPEC-platform, local stage). The `Today` values in the ratchet table are recorded at the first green CI run after that.

## Floor (always enforced, no setup required)

Checked by `python3 devtools/floor_guard.py` (diff-scoped against `origin/main`; exit `0` clean, `1` violation, `2` could not run, which never counts as clean).

- No new suppression comments: `# noqa`, `# type: ignore`, `# pyright: ignore` or a per-file downgrade, `pragma: no cover`, `@ts-ignore`, `@ts-expect-error`, `biome-ignore`, `v8 ignore`
- No unfinished work: `raise NotImplementedError`, a not-implemented throw, `except …: pass`, an empty `catch {}`, `TODO` or `FIXME` in added code
- No skipped, `.only`, xfailed, or deleted tests, and no assertions removed from tests that stay, without a reason in the commit message and a human's approval
- No CI step removed or softened (`continue-on-error: true`, `|| true`)
- No secrets in source: `.env` stays git-ignored and only `.env.example` is committed (SPEC-platform, Boundaries). Checked at review only. There is no scanner yet (see Considered, not enforced)
- This file is not weakened to make a change pass: a lowered threshold, a removed rule, or a new exception row is reported

`devtools/agentflow/tests/` is outside the removed-assertions check, and only that check. The suite pins the runner's own behaviour, so each deliberate change to the runner rewrites assertions: all 22 flagged up to 2026-11-02 were updates needing a manual approval each time. The rest of the floor still applies to it, agents can't edit it in a run (`guardrails.forbidden_paths`), and a human reviews those edits in the PR.

## Enforced with numbers

| Dimension | Rule | Checked by | Runs at | Blocks |
|-----------|------|-----------|---------|--------|
| Floor | Zero floor violations | `python3 devtools/floor_guard.py` | every task step, task end, CI | now |
| Types: backend | Zero pyright errors, strict on `src/` | `uv run pyright` (changed files while editing) | every edit, CI | from the skeleton |
| Lint: backend | Zero ruff errors, format clean | `uv run ruff check . && uv run ruff format --check .` | every edit, CI | from the skeleton |
| Types: console | Zero `tsc` errors, strict `tsconfig` | `pnpm exec tsc --noEmit` in `webadmin/` | every edit, CI | from the skeleton |
| Lint: console | Zero Biome errors, Biome defaults | `pnpm biome check .` in `webadmin/` | every edit, CI | from the skeleton |
| Architecture | Zero R15 import-rule violations | the import-boundary unit test in `tests/unit/platform/` (`uv run pytest tests/unit`) | task end, CI | from the skeleton |
| Coverage: backend | Changed lines ≥ 80% covered | `uv run pytest --cov=src/tivacare --cov-report=xml` (unit, then integration with `--cov-append`) + `uv run diff-cover coverage.xml --fail-under=80` | task end, CI | warns for 14 days after the skeleton, then blocks |
| Coverage: console | Changed lines ≥ 80% covered | `pnpm exec vitest run --coverage --coverage.reporter=cobertura` in `webadmin/` + `uvx diff-cover webadmin/coverage/cobertura-coverage.xml --fail-under=80` | task end, CI | warns for 14 days after the skeleton, then blocks |
| Eval gate | No drop past the fixed limits in `eval/baseline.json`, retrieval scored separately; a pull request that lowers the baseline past them needs a human's `Constraints-Approved-By:` trailer (ADR-031) | the CI `eval` job (`uv run python -m tivacare.eval.cli gate`; limits in `agent/policy.py`, SPEC-eval) | CI, on the R10 path list | when `eval` fills its slot |

Why these numbers:

- **80% of changed lines.** High enough that every change ships with a test, and low enough to allow a config line or an unreachable defensive branch. It covers changed lines only, so the agent is never blocked by coverage it inherited.
- **Zero type, lint, and boundary errors.** These tools are fast and deterministic, and there's no legacy code to grandfather in. Any number above zero just grows.
- **Coverage warns for its first 14 days.** That gives time to see what 80% means on this codebase before it blocks. During that window, `diff-cover` runs without `--fail-under`, and adding the flag at the switch date counts as tightening, so the guard stays silent. The date is set here when the skeleton lands.
- **Generated and non-shipping code doesn't count:** `webadmin/src/api/generated/`, migrations, and `devtools/` are left out of the coverage reports, so they never dilute or inflate the number.
- **`diff-cover` in the console job** runs through `uvx`, as a CI reporting step only. Building and testing `webadmin/` still needs no Python.

The other R10 gates in SPEC-platform also block: migrations from empty, the `.env.example` check, the generated-type drift checks, the simulator response-model test, the builds, and Playwright. They're defined there, not here, and weakening any of them is covered by the floor rule about CI steps.

## Measured, not yet enforced (ratchets)

| Metric | Today | Direction | Checked by |
|--------|-------|-----------|-----------|
| Backend project coverage (`src/tivacare`) | TBD at the first green CI run | must not fall | `uv run coverage report --fail-under=<Today − 0.5>` |
| Console project coverage (`webadmin/src`) | TBD at the first green CI run | must not fall | `vitest --coverage.thresholds.lines=<Today − 0.5>` |

A ratchet uses the recorded value, not a target. When the number improves, update `Today` (tightening, so silent). A drop of more than 0.5 percentage points is a finding. That tolerance absorbs drift when an unrelated file changes the total.

## Considered, not enforced

These were offered on 2026-10-19 and not chosen. Rerun `/constraint-driven-development` to add one.

- **Security scanning** (gitleaks `--redact`, osv-scanner on `uv.lock` and `pnpm-lock.yaml`, Semgrep `p/python` and `p/owasp-top-ten`). This is the gap that matters most. Every rule above is judged by this project's own tools or tests, so there's no outside opinion in the bar yet. gitleaks and osv-scanner run only in CI, so they're the cheapest to add, and gitleaks would give the floor's secrets rule a mechanism.
- **Console accessibility** (`@axe-core/playwright` in the existing e2e, zero critical or serious WCAG 2.1 AA violations).
- **Console bundle budget** (`size-limit` ratchet on the main entry). Lighthouse and LCP are skipped because the console is an internal tool.

## Lifecycle

| Phase | Skill | What runs | Budget |
|-------|-------|-----------|--------|
| Build | `executing-plans` / `subagent-driven-development` | Types and lint on the changed files, the floor guard | under 5 s |
| Verify | `verification-before-completion` | Unit tests, changed-line coverage, the boundary test | under 90 s |
| Review | the plan executor's final review | Everything above, plus the review watch-list below | minutes |
| Ship | CI on the PR | All rows, the R10 gates, the ratchets | unlimited |

In CI, the floor guard and `diff-cover` need the full history: check out with `fetch-depth: 0`.

**Review watch-list** (`git diff` at the branch point): a threshold, severity, or tool setting in `pyproject.toml`, `biome.json`, `tsconfig.json`, or the Vitest config moved toward looser; a test file deleted or its assertions thinned out; a new suppression or exception; a stub standing in for an implementation.

## Overrides

- **A tracked exception** is a row below, with IDs `W1`, `W2`, and so on. `Rule` is a floor-guard rule id (`silenced-checker`, `unfinished-work`, `test-made-easier`, `test-deleted`, `assertion-removed`, `ci-step-softened`, `ci-step-removed`) and `Path` is a glob. The guard honours unexpired rows, and an expired row makes it fail until someone removes the row or renews it. Expiry is at most 90 days out: long enough to plan the fix, short enough to remember it.
- **A one-off approval** is a `Constraints-Approved-By: <name>` trailer on a commit in the branch, with the reason in the message body. The guard still prints every finding, but it doesn't block. **Only a human adds this trailer. An agent never does.**

## Exceptions

| ID | Rule | Path | Reason | Owner | Expires |
|----|------|------|--------|-------|---------|
| — | none | | | | |
