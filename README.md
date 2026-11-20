# agentflow harness

A headless runner that drives Claude Code or Codex through a config-driven workflow (`define → plan → build → verify → review → ship`), ending with a PR ready for review.

Each step is one agent session (`claude -p` or `codex exec`). The runner, not the model, walks the phases, checks conditions from files and git, runs your quality gates, commits, pushes and opens the PR. You answer questions, approve the spec and the plan, and merge. agentflow never merges.

- **Project-agnostic engine, per-project pack.** The engine (`agentflow/`) names no project. A repository's `.agentflow/config.yaml` states only what differs from the engine defaults and a starter (`python-uv`, `node-pnpm`, `go` or `docs-only`).
- **Lanes.** `feature` (spec → plan → per-task build → verify → review → ship) and `bug` (debug → failing regression test → fix → verify → review → ship). A pack can add its own.
- **Safety.** Agents work in their own git worktree. They can't commit, push, edit the pack or touch forbidden paths, and only the runner commits. Budgets are hard stops.
- **Human in the loop, only when needed.** After `plan`, resolver sessions, refine rounds and CI fix rounds settle most stops. What's left goes to the inbox (`agentflow inbox`) or the local dashboard.
- **Learns from its runs.** `agentflow retro` proposes lessons from finished runs as a draft PR, and `agentflow lessons` and `agentflow scorecard` measure whether they helped.

## Requirements

Python 3.12+, [`uv`](https://docs.astral.sh/uv/), `git` with an `origin` remote, the GitHub CLI (`gh`) signed in, and Claude Code (or the Codex CLI) signed in. `agentflow doctor` checks all of them.

## Setup

Ask your coding agent to follow [SETUP.md](SETUP.md): *"Follow SETUP.md and set up agentflow."* It asks one short question at a time, shows the full plan once, then installs and configures everything. To set it up by hand, see [the playbook](docs/agentflow-playbook.md) (§3 one-time setup, §13 using agentflow in another repository).

The short version:

```bash
uv tool install --from /path/to/agentflow-harness agentflow   # once; add --reinstall after pulling engine changes

cd /path/to/your-repo
agentflow init --baseline          # writes a sparse .agentflow/config.yaml and runs your gates once on the base branch
agentflow doctor                   # agent CLIs and logins, gh, origin, Docker, the state dir, gate programs
agentflow validate                 # must print "0 warnings" (set gates_confirmed: true once you've checked the gates)
agentflow explain feature --module <id>       # what would run, with no agent calls
agentflow run feature --module <id> --dry-run # the exact commands; creates nothing
agentflow run feature --module <id>           # start; it stops when it needs you
agentflow inbox                    # answer / approve / reject what it asks
agentflow dashboard                # or use the local board (127.0.0.1, token in the URL)
```

## Documentation

| Read | For |
|---|---|
| [ARCHITECTURES.md](ARCHITECTURES.md) | how the engine is built: module map, run lifecycle, state, safety model, diagrams. Start here to contribute |
| [docs/agentflow-playbook.md](docs/agentflow-playbook.md) | operating runs: the cheat sheet (§10), troubleshooting (§12), other repositories (§13), every config key (§14), lessons and retros (§15) |
| [docs/agentflow-harness-proposal.md](docs/agentflow-harness-proposal.md) | the design and its rollout slices |
| [docs/specs/](docs/specs/) | one proposal per module (conformance, cost, doubt review, dashboard, …) |
| [docs/plans/](docs/plans/) | implementation plans, kept as records |
| [CLAUDE.md](CLAUDE.md) / [AGENTS.md](AGENTS.md) | rules for coding agents working in this repository |
| [CHANGELOG.md](CHANGELOG.md) | what changed in each version |

## Contributing

This repository runs agentflow on itself: its pack is [`.agentflow/config.yaml`](.agentflow/config.yaml), with the `feature` and `bug` lanes.

```bash
# the test file you touched (the full suite, agentflow/tests, takes 25-60 min: run it before you finish)
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_pack.py --import-mode=prepend -q

uvx ruff@0.17.4 check .        # required: CI runs this exact command
bin/agentflow validate         # runs the engine straight from this checkout
```

Before you open a PR, read [ARCHITECTURES.md §15](ARCHITECTURES.md#15-contributing-the-checklist-that-follows-from-the-architecture) and the invariants in [CLAUDE.md](CLAUDE.md). The main ones: stdlib + `pyyaml` only, no project names in the engine, the `explain` golden files never change, every config key ships with its Settings field, `validate` check, test and playbook line, and every user-visible change gets a `CHANGELOG.md` line.

## Changes

See [CHANGELOG.md](CHANGELOG.md) for what changed in each version.
