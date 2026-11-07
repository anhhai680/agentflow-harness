# SETUP.md: install and configure agentflow, guided by an agent

This file is for an AI coding agent (Claude Code, Codex, or similar). A user hands it to you with something like *"Follow SETUP.md and set up agentflow"*. Your job is to install the agentflow engine on this machine, configure it for one target repository, and prove that it works. Run it as one guided session:

1. **Discover** what you can find out without asking (Step 1).
2. **Ask** short questions, **one at a time**, only where you couldn't find the answer (Step 2).
3. **Show the full plan once** and get one "yes" (Step 3).
4. **Do everything** in the plan without stopping, unless a step fails (Step 4).
5. **Verify** and **report** (Steps 5 and 6).

The reference material is `docs/agentflow-playbook.md`: §2 prerequisites, §3 one-time setup, §6 Codex, §12 troubleshooting, §13 using agentflow in another repository, §14 the pack reference. Read a section with `grep -n '^## ' docs/agentflow-playbook.md` and then read only the section you need. If this file and the playbook disagree, the playbook wins.

---

## Rules for the whole session

- **One question per message.** Keep each one to a sentence or two. Give the options, mark the default with "(recommended)", and accept a short reply ("1", "yes", "default"). Never send a list of questions.
- **Don't ask for anything you can find out.** If Step 1 found the answer, state it in the plan and don't ask.
- **Show the plan once.** After the user approves it, don't ask for approval again step by step. Stop and ask only when (a) a step fails and the fix isn't obvious, (b) the step would delete or overwrite something the plan didn't name, or (c) a login needs the user's own hands.
- **Never weaken a gate** to make a check pass. The gates are the repository's quality bar, and the user owns them.
- **Never commit, push or open a PR** unless the user said yes to that in Step 2. Outside an agentflow run, the rule is "commit only when asked".
- **Never paste the dashboard URL** (it carries a token) into a chat, ticket or file.
- **Edit settings files by merging.** `.claude/settings.json`, `.claude/settings.local.json`, `~/.codex/config.toml` and the user config file may already hold the user's rules. Read the file first, add only your entry, and keep everything else.
- **Interactive logins are the user's.** You can't finish `claude` sign-in, `gh auth login` or `codex login` yourself. Ask the user to run the command with the `!` prefix (for example `! gh auth login`) so that its output lands in the session, then check it again.
- **Report what actually happened.** If something failed or was skipped, say so and show the output.

---

## Step 1. Discover (no questions)

Run these checks quietly and keep the results for the plan. None of them changes anything.

| What | How | Note |
|---|---|---|
| OS and shell | `uname -s`, `echo $SHELL` | macOS → offer `brew` installs; Linux → the official install scripts |
| Python 3.12+ | `python3 --version` | `uv` can provide Python itself, so a missing system Python isn't a blocker |
| `uv` | `uv --version` | required |
| `git` | `git --version` | required |
| GitHub CLI and login | `gh --version`, `gh auth status` | needed by `ship`, `--issue` and `queue` |
| Claude Code and login | `claude --version`, `claude auth status` | the default agent |
| Codex CLI (optional) | `codex --version`, `codex login status` | only if the user wants Codex |
| Is `agentflow` installed already? | `command -v agentflow`, `agentflow --help` | if it is, the install step becomes a reinstall |
| `~/.local/bin` on `PATH` | `echo $PATH` | `uv tool install` puts the command there |
| **Where the harness checkout is** | the directory holding this `SETUP.md`, `pyproject.toml` and `agentflow/flow.py` | this is `<harness>` below |
| **The target repository** | the current directory if it's a git repo and isn't `<harness>`; otherwise unknown | this is `<repo>` below |

For the target repository, if you know it, also check:

| What | How |
|---|---|
| A git repo with commits | `git -C <repo> rev-parse --show-toplevel`, `git -C <repo> log -1 --oneline` |
| `origin` remote and base branch | `git -C <repo> remote -v`, `git -C <repo> symbolic-ref refs/remotes/origin/HEAD` |
| Working tree state | `git -C <repo> status --short`, `git -C <repo> branch --show-current` |
| Already has a pack? | `ls <repo>/.agentflow/config.yaml` |
| Likely starter | `pyproject.toml` → `python-uv`; `package.json` → `node-pnpm`; `go.mod` → `go`; none of these → `docs-only` |
| Own skills | `ls <repo>/.claude/skills` (if it's missing, the bundled `agentflow-*` skills are used) |
| CI workflows | `ls <repo>/.github/workflows` (`init` reads gates from them first) |

---

## Step 2. Ask, one question at a time

Go through the questions in order. **Skip any whose answer Step 1 already gave you**, and skip any that the user's earlier answers make irrelevant. Wait for each reply before you send the next question. Most users answer five or six questions or fewer.

**Q1. Target repository** (ask only if Step 1 didn't find one)
> Which repository should agentflow work on? Give me its path. (Or say "this one" to set up the harness repository itself.)

If the target is `<harness>` itself, it already has a pack (`.agentflow/config.yaml`), so there's no `init`. The plan becomes: install, `doctor`, `validate`, `explain`, dry run.

**Q2. Existing pack** (ask only if `<repo>/.agentflow/config.yaml` exists and `<repo>` isn't `<harness>`)
> This repository already has an agentflow pack. 1) Keep it and just install and check it (recommended), or 2) delete `.agentflow/` and write a fresh one?

Option 2 deletes files. If the user picks it, name `.agentflow/` explicitly in the plan.

**Q3. Missing prerequisites** (ask only if something required is missing)
> `<tools>` aren't installed. Should I install them with `<brew | the official installer>`? 1) Yes (recommended), 2) No, I'll install them myself.

**Q4. How to install the engine**
> How should I install agentflow? 1) As a `uv` tool, so `agentflow` works from any directory (recommended), or 2) as a symlink to this checkout's `bin/agentflow`, so edits to the engine take effect right away (for people working on the engine).

**Q5. Agents**
> Which agents should runs use? 1) Claude Code only (recommended), or 2) Claude Code plus Codex (experimental; I'll add a Codex profile and enable it).

**Q6. Starter** (ask only if the detected starter is unclear, or there's more than one manifest)
> I'd use the `<detected>` starter (its gates: `<list>`). 1) Use it (recommended), or 2) pick one of `python-uv`, `node-pnpm`, `go`, `docs-only`.

**Q7. Baseline**
> Should `init` run the proposed gates once on `<base branch>` to find gates that already fail there? This runs the repository's own commands (for example its tests) in a throwaway worktree. 1) Yes (recommended if you trust the repository), or 2) No.

**Q8. Gate confirmation**
> After `init`, the gates are a proposal. 1) Show them to me at the end, and I'll confirm before you set `gates_confirmed: true` (recommended), or 2) confirm them automatically if every baseline gate passes.

Option 2 makes sense only if Q7 was "yes". If Q7 was "no", don't ask this question and use option 1.

**Q9. Bug-lane allow rule** (ask only if the bug lane stays on, which is the default)
> The bug lane needs a `Bash(...)` allow rule for the test command. Where should I add it? 1) `.claude/settings.json`, shared with the team (recommended), or 2) `.claude/settings.local.json`, just for you (git-ignored).

**Q10. Spending limit**
> Runs stop when their cost passes a budget, $25 by default. 1) Keep $25 (recommended), or 2) set a different amount (I'll put it in your personal config, not in the repository).

**Q11. Commit the pack**
> When setup passes, should I commit `.agentflow/config.yaml` on a new branch and open a PR so the team can review the gates? 1) No, leave it uncommitted for me (recommended), or 2) yes, branch, commit and open a PR.

---

## Step 3. Show the full plan once

Send one message with the whole plan, filled in from Steps 1 and 2. Use this shape and leave out the lines that don't apply:

```
Here's the plan. Nothing has changed yet.

Machine
  1. Install <uv | gh | claude | …> with <command>
  2. Install agentflow: uv tool install --from <harness> agentflow   (or: symlink <harness>/bin/agentflow → ~/.local/bin/agentflow)
  3. Logins you'll run yourself: <! gh auth login | ! claude | none needed>

Repository <repo> (base branch <base>, starter <starter>)
  4. agentflow init --starter <starter> [--baseline]     → writes .agentflow/config.yaml and nothing else
  5. Add the bug-lane rule "Bash(<test cmd> *)" to <.claude/settings.json | .claude/settings.local.json>
  6. [Codex] add [profiles.agentflow] to ~/.codex/config.toml; set agents.codex.enabled: true
  7. Gates: <show them to you at the end | confirm automatically if the baseline is green>
  8. [Budget] set defaults.run_budget_usd: <N> in ~/.config/agentflow/config.yaml

Check
  9. agentflow doctor · agentflow validate (0 warnings) · agentflow explain feature --module <id> · agentflow run feature --module <id> --dry-run

Finish
 10. <Leave the pack uncommitted | branch agentflow-setup, commit, push, open a PR>

Deletes: <none | .agentflow/ (you chose a fresh pack)>
Reply "go" to run all of this, or tell me what to change.
```

If the user changes something, update the plan, show it again, and wait for "go". After "go", don't ask again unless a rule above says to.

---

## Step 4. Do it

Run the steps in order. After each one, check that it worked before you move on. If a step fails, look up the cause in playbook §12, fix it if the fix is safe and obvious, and otherwise stop and ask one question.

### 4.1 Prerequisites

- **uv**: on macOS, `brew install uv`; otherwise `curl -LsSf https://astral.sh/uv/install.sh | sh`. Check with `uv --version`.
- **gh**: on macOS, `brew install gh`; on Linux, follow the instructions at https://cli.github.com. Then the user runs `! gh auth login`. Check with `gh auth status`.
- **Claude Code**: `npm install -g @anthropic-ai/claude-code` (or the installer the user prefers). Then the user runs `! claude` once to sign in. Check with `claude auth status`.
- **Codex** (only if Q5 = 2): `npm install -g @openai/codex`, then the user runs `! codex login`. Check with `codex login status`.
- If `~/.local/bin` isn't on `PATH`, run `uv tool update-shell` and tell the user to open a new shell. For the rest of this session, call the command by its full path (`~/.local/bin/agentflow`).

### 4.2 Install the engine

Option 1 (the `uv` tool):

```bash
uv tool install --from <harness> agentflow              # first install
uv tool install --reinstall --from <harness> agentflow  # if agentflow was already installed
```

Option 2 (the symlink, for working on the engine):

```bash
mkdir -p ~/.local/bin && ln -sf "<harness>/bin/agentflow" ~/.local/bin/agentflow
```

Check: `agentflow --help` prints the subcommands, including `init` and `doctor`.

### 4.3 Write the pack (skip this when the target is `<harness>`, or when Q2 = keep)

If Q2 = fresh pack: `rm -rf <repo>/.agentflow`, as the plan said.

From `<repo>`:

```bash
agentflow init --starter <starter> [--baseline]
```

`init` reads the base branch through git, writes `.agentflow/config.yaml` once, and runs `doctor`. With `--baseline` it also runs each gate once in a throwaway worktree. Keep the whole output, because you need three parts of it:

- **The `detected:` block and its `notes:`**: the gates, the gate sets, the docs folders and the skills. Any note about a skipped CI step goes into your final report.
- **The `baseline:` table** (with `--baseline`): a gate that fails on the base branch is written as `baseline: red`. A missing tool, a timeout or a failed setup shows as `error`, which isn't red. If it says *every verify gate fails*, runs can't pass verify until the user fixes them. Report that, and don't hide it.
- **The `bug lane:` line**: the exact `Bash(...)` rule to add in 4.4.

If `init` refuses because the repository already has a pack, go back to Q2. Don't delete `.agentflow/` unless the plan said so.

### 4.4 Add the bug-lane allow rule

Open the file chosen in Q9 (create it as `{}` if it doesn't exist). Add the rule `init` printed to `permissions.allow`, without duplicating it, and keep every other key:

```json
{
  "permissions": {
    "allow": ["Bash(uv run pytest *)"]
  }
}
```

Check that the file is still valid JSON (`python3 -m json.tool <file> >/dev/null`).

### 4.5 Codex (only if Q5 = 2)

1. In `~/.codex/config.toml`, add the following. Merge it, and don't replace a `[profiles.agentflow]` that's already there. Check the key names against `codex exec --help`.

   ```toml
   [profiles.agentflow]
   sandbox_mode = "workspace-write"
   approval_policy = "never"
   ```

2. In `<repo>/.agentflow/config.yaml`, set `agents.codex.enabled: true`. Leave the phases on Claude: moving a phase to Codex is a later, deliberate step (playbook §6).

### 4.6 Gates

- If Q8 = 1: don't touch `gates_confirmed` yet. In Step 6, show the gate table and ask the user to confirm it.
- If Q8 = 2 and every baseline gate passed: set `gates_confirmed: true` in `.agentflow/config.yaml`. If any gate was red or showed `error`, fall back to Q8 = 1.

Edit only that one line. `init` wrote the pack as plain YAML, so change the value in place and keep the comments.

### 4.7 Personal budget (only if Q10 = 2)

Write `defaults.run_budget_usd` in the user config file, `$XDG_CONFIG_HOME/agentflow/config.yaml` (or `~/.config/agentflow/config.yaml`), merging with any keys already there:

```yaml
defaults:
  run_budget_usd: 40
```

This file may hold personal keys only (playbook §14). Never put gates, commands or guardrails in it.

---

## Step 5. Verify

Run these from `<repo>`, in this order. Pick a short placeholder module id such as `setup-check` (any id works for `explain` and `--dry-run`, and neither one creates anything).

```bash
agentflow doctor                                       # each check prints ok / warn / fail; exit 1 means at least one fail
agentflow validate                                     # must end with "0 warnings"
agentflow explain feature --module setup-check         # which phases and steps would run, with no agent calls
agentflow run feature --module setup-check --dry-run   # the agent and gate commands; creates nothing
```

How to read the results:

- A `doctor` **fail** must be fixed before you call setup done. A **warn** is acceptable, but list it in the report.
- A `validate` warning about `gates_confirmed` is expected while the gates wait for the user (Q8 = 1). It's the only warning you may leave, and you must say so. Fix every other warning: the message names the YAML path to change.
- A `validate` warning about the bug-lane allow rule means 4.4 didn't take. Check the file and the rule text again.

If the target is `<harness>` itself, you may also run the fast portability gate. It's optional, and the full suite takes 25–60 minutes, so don't start that unless the user asks:

```bash
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_portability.py --import-mode=prepend -q
```

---

## Step 6. Finish and report

1. **Gates** (if Q8 = 1): show the gates, with their commands and baseline results, as a small table. Ask one question:
   > Are these gates the quality bar you want? 1) Yes, confirm them, 2) change them first (tell me what to change).

   On "yes", set `gates_confirmed: true` and run `agentflow validate` again until it prints 0 warnings. On changes, edit `gates:` and `gate_sets:` in the pack, then validate again.

2. **Commit** (only if Q11 = 2): create a branch (`git switch -c agentflow-setup`), stage only `.agentflow/config.yaml`, plus the shared `.claude/settings.json` if you changed it, and commit with one imperative sentence. Then push and open a PR with `gh pr create`, so the team can review the gates and the `permission_mode: bypassPermissions` line. Never stage `.claude/settings.local.json`.

3. **Report**, in one short message:
   - what you installed, and how (`agentflow` path and version);
   - the files you created or changed, each with its path;
   - the `doctor` result (any warns), and the fact that `validate` printed 0 warnings;
   - the gates, and anything `init` noted or skipped;
   - what's left for the user, if anything: a login, a red gate, uncommitted files;
   - the next commands:

   ```bash
   agentflow next                                  # what to do now in this repository
   agentflow run feature --module <id> --to define # a safe first run: the spec only, no PR
   agentflow inbox                                 # questions and approvals waiting for you
   agentflow dashboard                             # local board on 127.0.0.1 (keep its URL private)
   ```

   Also remind the user of two things. Agent runs spend real money, capped per run by `defaults.run_budget_usd`. And they approve `define` and `plan`, and merge every PR themselves: agentflow never merges.

---

## Quick troubleshooting

| Symptom | What to do |
|---|---|
| `agentflow: command not found` after the install | `~/.local/bin` isn't on `PATH`: run `uv tool update-shell` and open a new shell, or use the full path |
| The install serves old code | `uv tool install --reinstall --from <harness> agentflow` |
| `init`: *this repository already has a pack* | Go back to Q2. Delete `.agentflow/` only with the user's yes |
| `init`: *unknown starter* | Use one of `python-uv`, `node-pnpm`, `go`, `docs-only` |
| `doctor` fails on a login | The user runs `! claude`, `! gh auth login` or `! codex login`, then you run `doctor` again |
| `doctor` fails on a gate program | Install that tool, or fix the gate's command. Don't delete the gate to get past the check |
| `validate` names a YAML path | Fix that line and validate again (playbook §12) |
| The baseline says every verify gate fails | Report it. The user fixes the base branch or the gate commands. Runs can't pass verify until then |

Anything else: see playbook §12.
