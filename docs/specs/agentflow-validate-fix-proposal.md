# Proposal: `agentflow validate --fix` repairs the pack problems that have one right answer

Status: approved (2026-11-13)

## Objective

1. **A pack's prompt overrides fall behind the engine without a word.** A pack may override any engine prompt by
   putting a file of the same name in `.agentflow/prompts/`. A team usually starts from a copy of the engine's file
   and edits a few lines. When a later engine release changes or removes text in that file, the pack's copy keeps
   the old text, and nothing says so. Release 2.5 removed the `effort_change` result status and the effort-check
   block of `_preamble.md`. A consumer pack that overrode `_result.md` before 2.5 still listed `effort_change` as a
   status an agent may send, and a result that sends it is now refused with `effort_refused`. `agentflow validate`
   passed that pack with no warning about it: its only prompt check is for unknown `{placeholders}`.
2. **Nothing in agentflow repairs a pack.** `validate` reports and stops. `init` writes `config.yaml` once and
   refuses an existing pack (`ALREADY`), and it never writes prompts. Upgrading the engine leaves the operator to
   compare each override with the engine's copy by hand.

Success is that after an engine upgrade an operator runs `agentflow validate`. They see each override that still
carries text the engine retired, and `agentflow validate --fix` repairs every one that can be repaired with no
judgement. Those are the problems with one right answer.

## Decided behaviour (the user's choices)

- The command is **`agentflow validate --fix`**, not a new top-level verb. `update` would read as updating agentflow
  itself, and `fix` alone collides with the in-run `fix` step, `fix.md`, `fix_loop` and `fix_attempts`.
- `--fix` repairs **only what `validate` reports**. It is not a general pack rewriter or migration tool.
- `--dry-run` previews and writes nothing, as `run --dry-run` and `retro --dry-run` already do.
- It writes files and never commits. Like `init`, it leaves the change for the human to review and commit.

Settled while grilling the open questions (2026-11-13):

- **Accepting a deliberate warning is not in this slice.** A pack that switches a warning on purpose (such as
  `plan_approved: null`) still can't reach "0 warnings". Accepting a named warning with a reason needs a new config
  key, with its four obligations and a rule on which warnings may be accepted, so it is a separate follow-up slice.
- **The retired-text register is the only drift check.** No `<!-- from engine X.Y -->` stamp in overrides, and no
  stored history of engine prompts. Instead, every release that drops or rewords text in an engine prompt adds a
  `Retired` entry in the same change. That rule goes in `CLAUDE.md`'s Changelog section, next to the release steps,
  and the register self-check (see Testing) keeps the entries true.
- **An override identical to the engine's file is a `(fixable)` warning,** and `--fix` deletes it. A pack that had
  one goes from 0 warnings to 1 until it runs `--fix`. No pack in this repository has one.
- **`--fix` doesn't refuse a prompt with uncommitted changes.** It rewrites only exact engine text, prints its own
  diff, and checks before writing that the file still has the bytes it read, so no uncommitted work is lost.
- **Retired text is always a warning, never an error.** An engine upgrade must not suddenly fail a pack's `validate`,
  in CI or anywhere else, over text an agent may never act on. A run already refuses the retired `effort_change`
  status (`effort_refused`), and `--fix` repairs the text in one command.

## Design

### What is fixable in this slice

| Problem | `validate` reports | `--fix` does |
|---|---|---|
| A pack prompt holds text the engine **retired**, and the text is still **exactly** what the engine shipped | warning, marked `(fixable)` | replaces it with the engine's current text, or removes it when the engine dropped it outright |
| A pack prompt holds retired text that the team **edited** (the exact old text is not there, but its detection token is) | warning, not fixable, naming the engine version that retired it and the token | nothing; the warning says what to change |
| A pack prompt is **byte-identical** to the engine's current file of the same name | warning, marked `(fixable)`: the override changes nothing and will hide later engine fixes | deletes the override, so the engine's file applies |

Every other `validate` issue stays as it is, with no fix. Those issues need a person: errors such as an
`effort.allowed` with no level at or above `medium` or a reserved step id, and warnings such as `gates_confirmed:
false`, a red baseline or an obligation switched off.

### The retired-text register

A new engine module, `agentflow/pack_fix.py` (stdlib only), holds a tuple of `Retired` entries. Each entry
describes one piece of prompt text that a release removed or replaced:

```python
@dataclass(frozen=True)
class Retired:
    id: str                 # stable, e.g. "effort-change-status"
    retired_in: str         # the engine version that dropped it, e.g. "2.5"
    detect: str             # a token whose presence in any pack prompt raises the warning, e.g. "effort_change"
    old: str                # the exact text the engine shipped before `retired_in`
    new: str                # what replaces `old` ("" removes it)
```

This slice ships two entries, both from 2.5:
- `effort-check-block`: the block from `<!-- effort-check -->` to `<!-- /effort-check -->` in the 2.4
  `_preamble.md`, replaced by nothing.
- `effort-change-status`: the 2.4 `status` line of `_result.md`, replaced by the 2.5 line.

Detection runs on every file in the pack's `prompts/` (`pack.prompt_files`), not only the file the entry came from,
because a team may paste a shared partial into another prompt. The fix applies only where `old` matches exactly,
with line endings normalised. A partial or edited match is never rewritten.

From this slice on, a release whose `### Removed` or `### Changed` changelog entry drops or rewords text in an engine
prompt adds a `Retired` entry in the same change. `CLAUDE.md`'s Changelog section states this rule.

Every `Retired` entry is a warning. There is no per-entry level (see Decided behaviour).

### The CLI

- `agentflow validate` prints the new issues like any other issue, and fixable ones end with `(fixable)`. When at least
  one is fixable, the last line adds `N fixable: run agentflow validate --fix`. The exit code rules don't change.
- `agentflow validate --fix --dry-run` prints a unified diff for each file it would change, and `delete <path>` for
  each override it would remove. It writes nothing. The exit code is the same as `validate`'s.
- `agentflow validate --fix` applies the fixes, prints the same diff, then runs `validate` again and prints that
  result. The exit code is the second validation's.
- `--dry-run` without `--fix` is a usage error.

### Safety (this writes into `.agentflow/`)

- **Never inside a run.** `--fix` refuses when `AGENTFLOW_RUN` is set: an agent in a run may not change the pack,
  and the pack is always the main checkout's, outside the run's worktree.
- **Only the pack's own prompt files.** It writes only regular files directly under the pack's `prompts/`, as listed
  by `pack.prompt_files`. Each write and delete goes through `safeio` (`write_text`, `remove`), which never follows
  a symlink. A symlinked prompt is refused and named.
- **Compare before write.** It reads each file, computes the fix, and before writing checks that the file still has
  the bytes it read. If it changed, that file is skipped and named, and the other fixes still apply.
- **No `config.yaml` edits in this slice.** None of the fixable problems lives there. A later fix that renames a
  retired config key will go through `config_edit`'s comment-keeping edit, not a new writer.

`pack_fix.py` writes inside the repository from data the repository supplies, so the slice is planned and reviewed
with `security-and-hardening` and the `security-auditor` agent, as for the overlay's `when_paths`.

### Docs

- `CHANGELOG.md` gets an `Added` line for `validate --fix` and an `Added` line for the two new warnings.
- The playbook gets `validate --fix` in §10 (the cheat sheet), and a short "after an engine upgrade" note in §13.
- The `CLAUDE.md` command list gets `bin/agentflow validate --fix [--dry-run]`, and its Changelog section gets the
  register rule.
- This slice adds no config key, so the four obligations per key don't apply.

## Boundaries

- **Always:** keep the register in step with the engine's prompts (see Testing). Fix only what matches exactly.
  Print every change before or as it is made.
- **Ask first:** any fix that touches `config.yaml`, any fix that is not an exact text match, and any new kind of
  fixable problem beyond the two above.
- **Never:** commit, push or stage. Write outside `.agentflow/prompts/`. Run inside an agentflow run. Rewrite text a
  team edited. Change the `explain` golden files.

## Testing

A new `agentflow/tests/test_pack_fix.py` runs on a copy of the `python` fixture pack, test first:

- A pack `_result.md` with the 2.4 status line: `validate` warns `(fixable)` and names 2.5. `--fix --dry-run` prints
  the diff and leaves the file byte-identical. `--fix` rewrites it, and the second validation has no such warning.
- A pack `_preamble.md` with the 2.4 effort-check block: `--fix` removes exactly the block and keeps the rest.
- The `effort_change` token in an edited line: the warning is there, it is not fixable, and `--fix` leaves the file
  untouched.
- A pack prompt identical to the engine's: a `(fixable)` warning, and `--fix` deletes it.
- A symlinked prompt is refused. A file changed between read and write is skipped and named. `AGENTFLOW_RUN` set
  refuses `--fix`. `--dry-run` without `--fix` is a usage error.
- **Register self-check:** for every `Retired` entry, the current engine prompts contain neither `old` nor `detect`,
  and a non-empty `new` is present in the engine prompt it came from. That keeps the register honest when a later
  release edits a prompt again.

`test_portability` must still pass, since the register holds only engine text. The `explain` goldens must be
unchanged, and `agentflow validate` on this repository's own pack must still print 0 warnings.

## Open Questions

None. The four questions this proposal first raised are settled under Decided behaviour.
