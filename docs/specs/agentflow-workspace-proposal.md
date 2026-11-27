# Proposal: a user-level dashboard and CLI that work on several repositories of a workspace at once

Status: approved (2026-11-26; decisions settled by grilling the same day, see "Decisions")

Builds on `docs/agentflow-harness-proposal.md` §1b (engine and pack: one pack per repository) and §1c (plug and play,
config layers), `docs/specs/agentflow-throughput-proposal.md` (the usage-window governor is already machine-wide) and
`docs/specs/agentflow-dashboard-ux-proposal.md` (the dashboard's tabs). It changes no lane and no run semantics: a run
still belongs to exactly one repository and one pack.

## Objective

### The problem

A developer's *workspace* is a folder of sibling repositories (a service, its client, a shared library, the infra
repo, …). agentflow sees one repository at a time:

1. **The CLI is bound to the working directory.** `flow._find_repo()` resolves `Path.cwd()` to a main checkout, and
   every command works on that one repository. There is no `-C <path>` flag, and nothing lists the runs of two
   repositories together.
2. **The dashboard is a per-repository tool.** `agentflow dashboard` calls `_find_repo()` first, so it cannot start
   outside a repository, and `DashboardServer` holds one `repo` and one `cfg` (43 uses of `server.repo` /
   `server.cfg` in `dashboard.py`). A run id is looked up under one `runs/` root, the SSE stream tails one runs root,
   and `/api/info` is a constant. A developer with four repositories runs four dashboards: four tokens, four browser
   tabs, and a default port (`dashboard.port: 7788`) that every dashboard after the first fails to bind.
3. **Nothing says what needs me across repositories.** The inbox, the agents view and the notification digest are per
   repository, so four repositories with one run each means four places to look.
4. **Settings has one level.** The Settings page edits one repository's pack. A personal value (a path on this
   machine, a budget, a port) shows there as read-only ("the page never writes a personal value into the pack"), and
   a setting that belongs to no repository has nowhere to live.
5. **What already works across repositories is invisible.** State is isolated per repository
   (`<state>/agentflow/<repo-id>/`, with `repo-id` = name + path hash) and the usage-window governor is machine-wide
   per Claude account. Parallel runs in different repositories already share one budget and cannot collide. A
   developer with two terminals can already run two repositories side by side, but can't see them together.

### What we build

- **Workspace settings at user level**: `workspace.roots`, `workspace.repos` and `workspace.exclude`, personal keys in
  the user's own `config.yaml` (`~/.config/agentflow/config.yaml`), never in a pack.
- **A user-level dashboard**: `agentflow dashboard` runs from any directory, needs no repository, and when a workspace
  is configured serves every repository in it from one process, one port and one token. The developer picks a
  repository on the page and can switch at any time; every repository stays live in the background, so runs go on in
  parallel and a waiting item in another repository is never missed.
- **Two-level Settings**: a setting that is personal (the workspace, the dashboard's own settings) is saved at user
  level, and every other setting is saved to the selected repository's pack. The page shows where each field saves.
- **`-C <path>` on every command**, and **`--all` on the read-only views** (`status`, `inbox`, `agents`, `next`,
  `doctor`), which read the workspace settings, work from any directory and print each repository's output under a heading that names it.
  `agentflow workspace list` shows what the workspace resolved to.
- **No workspace configured means no change**: `agentflow dashboard` inside a repository serves that repository,
  with today's routes and page.

### Who it is for, and what success looks like

The developer who keeps three to ten repositories under one folder and runs agentflow in several of them in a day.
Success is that they set the folder once (in the file or on the Settings page), open one dashboard from anywhere,
see every repository's waiting items and live agents in one place, answer or approve any of them without another
tab, switch to any repository's Board in one click, and run `agentflow inbox --all` instead of cycling through
directories.

### Non-goals

- **No cross-repository runs.** One run, one repository, one pack, one worktree set. A run never reads or writes
  another repository.
- **No cross-repository dependencies** (an item in repo A that depends on an item in repo B). That needs claims and a
  DAG that span repositories and is a separate proposal.
- **No shared pack, shared lessons or workspace-level pack.** Each repository keeps its own `.agentflow/`.
- **No machine-wide cap on simultaneous runs and no merged notification digest** in the first slices. Both are
  conditional (slice W5) on real use showing the need.
- **No combined Board across repositories.** Lanes, phases and gates differ per pack.
- **No login service.** `agentflow` is already a user-level tool (`uv tool install`); the dashboard just has to run
  from anywhere. A launchd/systemd service is a later proposal, because runs started from a login service outlive a
  terminal.
- **No remote or multi-user dashboard.** It stays on 127.0.0.1 with one token.
- **No `agentflow run --all`, no cross-repository `queue`.** Starting work is deliberate and per repository.

## Assumptions

Correct these before approving.

1. A "workspace" is one or more folders whose child folders are git repositories, plus any repository added by path.
2. A repository with no `.agentflow/config.yaml` is listed as "not set up" and never written to. The dashboard never
   runs `agentflow init` (it runs the baseline gates, so it stays a deliberate terminal action).
3. The runner for one run is still the process `agentflow run` or the dashboard's spawner starts. Nothing here makes
   one long-lived process own several repositories' runs.
4. The dashboard's trust model is unchanged: 127.0.0.1, a 128-bit token, a same-site check on every request.
5. Repositories are the developer's own and already trusted enough to run agents in. The dashboard adds no new
   permission: starting a run in repository B uses B's pack and B's permissions, never A's.

## Design

### 1. Workspace settings (user level)

Three new personal keys, added to `layers.USER_KEYS`:

```yaml
# ~/.config/agentflow/config.yaml
workspace:
  roots:                     # folders whose immediate child git repositories join the workspace
    - ~/MyWorkplace/myprojects
  repos:                     # repositories outside any root, by path
    - ~/code/shared-infra
  exclude:                   # repositories to hide: a path, or a folder name
    - scratch-experiments
```

- **User layer only.** A pack that sets `workspace.*` is ignored for the workspace and `validate` warns
  ("`workspace.roots` is a personal setting; set it in your user config"). A committed pack must not carry one
  developer's absolute paths.
- **Resolution (`agentflow/repo_set.py`, new; `agentflow/workspace.py` already holds the per-run agent directory).** Each root is expanded one level deep: its immediate children that
  are git checkouts. Hidden folders and symlinks are skipped, like `layers.starter_names`. Every entry goes through
  `pack.checkout_root`, so a linked worktree resolves to its main checkout, and entries that resolve to one checkout
  collapse to one. `exclude` is applied last.
- **Identity.** A repository's key is `pack.repo_id(root)` (name + 8-hex path hash): stable, unique, filesystem-safe
  and already the state dir's name. The display name is the folder name; two repositories with one name show
  `name (parent)`. URLs carry the id.
- **Never an error for the rest.** A missing path, a path that is not a checkout, or a repository with no pack is a
  `Skipped(reason)` entry. A repository with a pack is **live**; one without is **not set up** (shown greyed with the
  hint `agentflow init`); one that disappeared while runs are in flight stays listed as **missing** until they end and
  is never deleted.
- **Rescan** every minute and when the workspace settings are saved, so a repository cloned a moment ago appears
  without a restart. A rescan is one directory listing plus a pack-file existence check per repository. It opens and
  runs nothing inside a repository (the same rule as `detect.py`: the repository is untrusted input).
- The dashboard's own personal keys join the user layer in the same change: `dashboard.board_refresh_s` and
  `dashboard.done_shown` (`dashboard.port` already is one; `dashboard.host` stays protected at 127.0.0.1).

### 2. A user-level config loader

`load_config(path)` needs a pack: it reads `path` as the pack, checks `version`/`engine`, and `validate` refuses a pack
with no `gate_sets.verify`. A dashboard or a CLI view that is not tied to a repository has no pack to load.

Add `load_user_settings()` (in `layers.py` or a thin function next to it): the engine defaults plus the user file, merged
by the existing `layers.merge`, returning a small typed `UserSettings` with `workspace` and the process-level
`dashboard` values (`port`, `board_refresh_s`, `done_shown`). It is read-only, rejects what `validate_user_file` already
rejects, and never loads a pack. A user file that fails to load makes the dashboard start with the engine defaults and
show the error on the Settings page. When the file parses as a YAML mapping, the page's Your settings section stays
editable and shows the error, so a bad entry (a relative `workspace.roots` path, say) is fixed from the page. When
the file is a folder, a symlink, a hardlink, not UTF-8 or not YAML, the section shows the reason with every field read
only, and the file is fixed by hand (W4).

### 3. The CLI

- **`-C <path>`** is a top-level option (`agentflow -C ~/code/billing status`): the directory `_find_repo()` starts
  from, instead of `Path.cwd()`. The default is unchanged. An unreadable or missing path is
  `error: -C <path>: <reason>`; a path that exists but is in no checkout reaches the command, which gives today's
  `not inside a git checkout` error.
- **`--all`** on `status`, `inbox`, `agents`, `next` and `doctor` runs the command once per workspace
  repository, in workspace order, from inside that repository (the command's own code is not changed, so its output
  is exactly what it prints today). Each repository's output follows a heading, `== <name> (-C <path>) ==`, which also
  gives the option to put in front of any command the output suggests. A repository with no pack prints
  `not set up` under its heading. A repository whose pack fails to load prints its own `error:` line and the loop goes
  on; the exit code is 1 when any failed. Only a command that loads the pack reports it (`agents`, `next`, `doctor`):
  `status` and `inbox` read runs and never load the pack, as today. They work from any directory. With no workspace configured `--all` stops with
  `no workspace: set workspace.roots in ~/.config/agentflow/config.yaml`. `--all` with `-C` is refused.
- The view functions already take `(repo, cfg)` (`views.agents_view`, `hitl.inbox_items`, `state.list_runs`), so
  `--all` is a loop, not a change to them.
- **`agentflow workspace list [--json]`** (read-only) prints the roots, each repository found with its state (live, not
  set up, missing), what was skipped and why. There is no `workspace add|scan|remove`: the workspace is changed in the
  config file or on the Settings page.
- **Write commands stay single-repository**: `run`, `resume`, `queue`, `answer`, `approve`, `reject`, `cancel`,
  `retry` take `-C` but not `--all`; run ids are unique only inside one repository. A repository's heading carries
  the `-C <path>` option (shell-quoted, so it can be pasted) to put in front of the `agentflow answer`, `approve` or
  `reject` command its output suggests.

### 4. The dashboard

#### 4.1 Start-up

| Where it runs | Workspace configured | Result |
|---|---|---|
| inside a repository | no | that repository only, exactly as today |
| inside a repository | yes | the workspace; that repository is preselected when it is in it |
| outside any repository | yes | the workspace; the first repository is preselected |
| outside any repository | no | opens on **Settings → Workspace** with an "add a folder" prompt, never `not inside a git checkout` |

The port comes from the user level (`dashboard.port`, default 7788, `--port 0` as today). A port in use fails with the
existing `OSError` plus the hint `another dashboard may be running; use --port 0`. One process, one port, one token
per user.

#### 4.2 Server shape

```python
@dataclass
class RepoContext:
    id: str            # pack.repo_id(root)
    name: str
    repo: Path         # main checkout
    cfg: Config        # that repository's pack, reloaded when its Settings page saves, as today
    config: Path | None
    state: str         # live | not_set_up | missing

class DashboardServer(ThreadingHTTPServer):
    repos: dict[str, RepoContext]      # workspace order = the switcher's order
    default: str | None                # what the legacy routes resolve to; None when there is no repository
    user: UserSettings                   # the process-level settings
```

Every handler that reads `self.server.repo` / `self.server.cfg` receives a `RepoContext` and uses `ctx.repo` and
`ctx.cfg`. `make_server(repo, cfg, …)` keeps its signature and wraps the pair in a one-entry map, so every single
repository caller and `test_dashboard.py` keep working. A repository whose pack fails to load becomes a context with
`state: "error"` and the error text, never a server that fails to start.

Every live repository stays **live in the background**, whichever one the page shows: reminders and preflight
rechecks (local, cheap), the waiting-item counts (local state), and the Backlog, issue and alert refresh. Network
refreshes run **one repository at a time** in one thread, so `gh` load stays flat; a repository's failed refresh is
logged with its name and never stops the others. A rescan (1) adds or retires contexts.

#### 4.3 Routes

| Route | Meaning |
|---|---|
| `/api/repos` | the workspace: `id`, `name`, `state`, pack version and counts `waiting`, `active`, `failed`; plus the skipped entries |
| `/api/repos/<id>/…` | every route the legacy API serves, with that repository's context |
| `/api/inbox`, `/api/agents` (legacy paths) | one repository: unchanged and untagged. When the server holds more than one repository (whatever their states), they merge every usable repository's items, each with a `repo` field, and need no default: they answer while the default is broken or gone. In W2 they were the default repository's, untagged. `/api/repos/<id>/inbox` and `/api/repos/<id>/agents` serve one repository's, tagged with `repo` in the same case |
| every other legacy `/api/...` path | resolves to `default`; with no `default`, 404 |
| `/api/user-config` (GET), `/api/user-config/preview` and `/api/user-config/save` (POST) | the user-level settings form, and its preview and save (section 5). They need no repository, as `/api/repos`, and are 404 under `/api/repos/<id>/`. They take form changes only (a body with `text` is a 400). A save answers `{ok, sha256, restart, rescanned}`; `restart` lists the saved keys that need a restart |
| `/api/events` | one SSE stream; each message gains `repo`. In W2 the legacy `/api/events` is the default repository's stream, untagged, `/api/repos/<id>/events` is one repository's, tagged, and the merged, tagged stream is `/api/events?all=1`; W3 moved the page to it, and it needs no default |

`<id>` is looked up in the server's own map, **never** turned into a path. A request can reach only a repository the
last rescan named. A run id is resolved under that repository's `runs/` root by the existing `_run` check (regex, no
symlink, parent equals the root), so a run id of A sent to B is a 404.

Until W3 moved the page, `/api/inbox`, `/api/agents` and `/api/events` (the legacy paths) stayed the default
repository's, byte for byte: the W2 page would show another repository's inbox rows that its actions cannot reach,
and would refresh the wrong card for a run id that two repositories share. The merged, tagged stream is
`/api/events?all=1` from W2.

#### 4.4 The page

- A **repository switcher** in the header: a chip per repository with a badge for the items waiting on a person in it,
  and a greyed "not set up" group. The selection is kept in `location.hash`, as a `#/repo/<id>/…` prefix on every route (`#/repo/<id>/board`), and in
  `sessionStorage`. It
  only changes what the page shows; nothing is paused.
- **Board, Workflow, Metrics, Lessons, run drawers**: per repository. `core.js`'s request helper prefixes paths with
  `/api/repos/<id>`.
- **Inbox and Agents**: an **All repositories** chip lists every repository's items with the repository as the first
  column. Acting on an item (answer, approve, reject, pause, cancel) posts to that item's own repository, never the
  selected one.
- **SSE**: one `EventSource`. Every event updates its repository's badge; the page renders only the selected
  repository's runs.
- With one repository there is no switcher and no extra column. The tab title names the repository (`agentflow · <name>`)
  or counts them (`agentflow · <n> repositories`); it comes from `/api/repos`, and `/api/info`'s `name` stays the harness
  name.

### 5. Two-level Settings

The Settings page gets two scopes and routes each field by what it is:

| Field | Saves to |
|---|---|
| `workspace.*`, `dashboard.port`, `dashboard.board_refresh_s`, `dashboard.done_shown` | the **user file** |
| the other personal keys the page already has fields for (budgets, `agents.claude.models` and `agents.codex.models`, `hitl.remind_after_h`, the `usage.*` keys) | the **user file**, from the Your settings section. The repository's form keeps these fields too, and they save to the **pack** (a pack states its team's defaults); a field the user file sets is read-only there and points to Your settings |
| every other key | the **selected repository's** `.agentflow/config.yaml`, as today |
| `hitl.notify`, `agents.*.user_settings`, `dashboard.host`, everything in `_PROTECTED_KEYS` | **not editable on the page** (by hand only) |

- A **Your settings** section is visible whenever the page is open, including with no repository, and each field shows
  a badge, `saves to: your user config` or `saves to: <repo>`. A field whose value the user file sets, which is
  read-only in a repository's form, is editable in Your settings (the personal-value rule: the page never writes a
  personal value into a pack that the user file already sets, and now has a place to write it instead). The
  repository's form keeps its note and adds a line, "change it under Your settings".
- **The write path to the user file is new and is the one security-critical piece.** It reuses `config_edit`'s
  machinery (comments and anchors kept, a sha256 conflict check, an atomic write) with these limits: only the user
  file, `layers.user_config_path()`; only keys on a fixed allow-list; the new text is loaded through
  `load_user_settings()` and the whole stack before it is written, and a text that would not load is refused; in every
  live repository whose pack loads with the file on disk, the new text must also load and pass `validate` wherever
  it passed before (an error at a key that had none is refused and names the repository, so a save never breaks a
  live run's reload or `agentflow validate`; a repository that already fails blocks nothing); a symlink, a hardlink or
  anything that is not a regular file is refused by `config_edit.read_user` (opened with `O_NOFOLLOW`, then `fstat`
  and `st_nlink` on the descriptor), and the folder is made by `safeio.ensure_dir`, which refuses a symlinked one;
  every `workspace.*` entry keeps the shape `layers.workspace_settings` takes (`/…`, `~` or `~/…`), and only a
  `workspace.roots` entry the file does not list yet must be a folder that exists (one already listed may have gone
  away, and a save of another field still works).
- A change to `workspace.*` re-runs the rescan at once. A change to `dashboard.port` takes effect on restart, and the
  page says so.

### 6. Notifications

The default notification title gains the repository name (`agentflow · billing`) when the user file names a workspace (`roots` or `repos`). The title is
built in the runner's process, which does not know which repositories are live, and finding out would cost a
`git rev-parse` per child on every notification; the user file is already read, so it decides. A `title` set in
`hitl.notify` is kept as written and gets no label.
`hitl.digest` stays **per repository**: `digest_text` counts one repository's runs and writes one file under that
repository's state dir. A merged digest is conditional (W5).

### 7. Concurrency across repositories

| Resource | Today | This proposal |
|---|---|---|
| Run state, worktrees, workspaces, claims, lessons | per repository (`<state>/<repo-id>/`, that repository's refs on `origin`) | unchanged; they cannot collide |
| Claude usage windows (5 h / 7 d) | machine-wide per account (`usage_window.json`, waiter ranking in `governor.py`) | unchanged; already correct across repositories |
| Agent CLI process, `caffeinate` | one per run | unchanged |
| CPU, memory, Docker, local ports used by gates | not governed | no cap for now; W5 adds `workspace.max_active_runs` only if real use shows the problem |
| `gh` API rate limit | one process per repository | the dashboard refreshes one repository at a time (4.2) |

### 8. Doctor and validation

- `agentflow doctor --all` runs the existing checks per workspace repository, under its heading. A plain `doctor` also
  reports the user-level workspace: an entry that was skipped (a missing root, a path that is not a git checkout) and
  each repository that is not set up. Two entries that resolve to one checkout collapse silently.
- `validate` gets checks for the new keys: `workspace.roots` / `repos` / `exclude` are lists of non-empty path strings;
  a pack that sets one warns (ignored in a pack); `dashboard.board_refresh_s` and `done_shown` keep the ranges the
  page enforces today (0.5–60 seconds, an integer 1–1000).
- `agentflow init`, in a repository outside the workspace, prints one line: add its folder to `workspace.roots`.

## Security

The dashboard can start agents with durable permission, and it now spans repositories and writes a file outside any
repository, so these rules are stated and tested:

- **A request names a repository only by an id the server's own map holds.** No request value becomes a path. Only a
  rescan, driven by the user file, changes the map; a request cannot add a repository.
- **Context never crosses.** A handler receives one `RepoContext` and uses its `repo`, `cfg` and `config` together. A
  test starts a server over two toy repositories, one of which forbids a path the other allows, and checks that each
  repository's guard and `_PROTECTED_KEYS` decide on its own pack.
- **The repository's Settings form edits that repository's `.agentflow/config.yaml` only**, through `config_edit` with
  that context's `cfg.path`. `.agentflow/**` stays forbidden to every agent in every repository.
- **The user-file write (section 5) is the new write surface.** It is limited to one file and an allow-list of
  low-risk personal keys (`config_edit.USER_FIELD_KEYS`); `hitl.notify` (it runs commands), `agents.*.user_settings`
  (it widens what an agent reads), `dashboard.host` and every key in `_PROTECTED_KEYS` can never be set through the
  page. The one exception is `workspace.*`: it is in `_PROTECTED_KEYS` to keep it out of a *pack*, and it is the
  allow-list's own for the user file. A key outside the allow-list is a 400, whatever the body says. The user-file
  path is never weaker than the pack's: a save must load with, and pass `validate` in, every live repository where
  it passed before. The existing same-site check and token apply unchanged.
- **One token for all repositories means a leaked token reaches all of them**, where today it reaches one. The token
  stays 128 bits, 127.0.0.1 only, never logged. The start-up note says how many repositories it covers. This is the
  one real widening of the blast radius, accepted for the productivity gain.
- **`workspace.*` values are untrusted input**: `~` and absolute paths only, resolved through `checkout_root`, symlinks
  skipped, never executed, never passed to a shell. A rescan reads directory names and a pack file's existence only.
- **`layers.py` and `config_edit.py` change** (new user keys, `load_user_settings`), and `layers.py` is on the pack's
  `overlays.security.when_paths`. Slices W1 and W4 are therefore planned with `security-and-hardening` and reviewed with
  the `security-auditor` agent. Slices W2 and W3 (dashboard request handling and token scope) get the same review.

## Commands

```bash
# tests, from the repository root
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_repo_set.py agentflow/tests/test_workspace_cli.py --import-mode=prepend -q
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py agentflow/tests/test_dashboard_js.py --import-mode=prepend -q
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_config_edit.py agentflow/tests/test_layers.py --import-mode=prepend -q
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_portability.py --import-mode=prepend -q
uvx ruff@0.17.4 check .

# what a developer runs (from any directory)
agentflow workspace list
agentflow inbox --all
agentflow -C ~/MyWorkplace/myprojects/billing-service run feature --module invoices
agentflow dashboard
```

## Project structure

```
agentflow/repo_set.py             resolve roots/repos/exclude into repositories and Skipped entries (new)
agentflow/layers.py               the new USER_KEYS, load_user_settings / UserSettings
agentflow/flow.py                 -C, `workspace list`, --all, a dashboard that starts with no repository
agentflow/dashboard.py            RepoContext, the repository map, rescan, /api/repos, /api/user-config, the loops
agentflow/config_edit.py          the user-level form and its guarded write
agentflow/dashboard_ui/           repos.js (switcher, badges; new), core.js (path prefix), inbox.js, agents.js, settings.js
agentflow/hitl.py                 the notification title carries the repository name
agentflow/tests/test_repo_set.py, test_workspace_cli.py   resolution, rescan, -C, --all (new)
agentflow/tests/{test_dashboard,test_dashboard_js,test_config_edit,test_layers}.py   extended
agentflow/tests/fixtures/packs/{python,node}       the two toy packs double as a two-repository workspace
docs/agentflow-playbook.md        §10 cheat sheet, §14 one line per new key, a new section "Several repositories" (§17)
CHANGELOG.md, pyproject.toml, agentflow/pack.py     one entry and one version bump per slice
```

## Code style

The repository's own: stdlib plus `pyyaml`, type hints, one-line imperative docstrings that say the rule, small pure
functions that take `(repo, cfg)` and return data, and the I/O in `safeio` / `gitops`.

```python
def resolve(settings: WorkspaceSettings) -> tuple[list[RepoRef], list[Skipped]]:
    """The workspace's repositories as main checkouts, and each entry that was skipped with its reason.

    A bad entry is a `Skipped`, never an exception: one broken path must not hide the others."""
```

## Testing strategy

No test calls a real model or GitHub (the fake agent and fake `gh` stay). New tests:

- **`test_repo_set.py`**: a root expands one level deep and skips hidden folders and symlinks; two entries that resolve
  to one checkout (a linked worktree included) collapse; `exclude` wins; a path that is not a checkout is a `Skipped`;
  a repository with no pack is `not_set_up`; a removed one is `missing`; nothing is run in a scanned repository.
- **`test_layers.py`**: the new keys are accepted in the user file and refused elsewhere as personal; `load_user_settings`
  merges the engine defaults and the user file with no pack; a broken user file falls back to the defaults with the
  error attached.
- **CLI**: `-C` resolves the same repository as `cd` (a linked worktree resolves to its main checkout);
  `status|inbox|agents|next|doctor --all` over the two toy packs, run from a directory that is no repository, prints a
  heading per repository, keeps going past one broken pack and exits 1; `--all` with no workspace gives the message; `--all`
  with `-C` is refused; the existing single-repository output is byte-identical (the golden `explain` files and the
  current `status` tests do not change).
- **Dashboard**: a server over the two toy repositories. `/api/repos` lists both with counts; a run id of A under
  `/api/repos/<B>/runs/<id>` is 404; an unknown repository id is 404 and no request value reaches a path; an approve
  for an item of A changes A's run and not B's; the SSE stream tags events with `repo`; one repository's refresh
  failing is logged and the others refresh; a rescan adds a new repository and retires a missing one only after its
  runs end; the server starts outside a repository, and with no workspace opens with the empty state; a
  one-repository server serves the legacy routes exactly as before.
- **Dashboard, W3** (done in 3.16): the legacy `/api/inbox` and `/api/agents` return each item with `repo` when the server holds more than one repository (whatever their states), and
  the page's stream is `/api/events?all=1`.
- **Settings** (`test_config_edit.py`, done in 3.17): a `workspace.*` save lands in the user file and not in any pack; a project field
  lands in the pack; `hitl.notify`, `agents.*.user_settings`, `dashboard.host` and any `_PROTECTED_KEYS` key sent to
  `/api/user-config` are 400 and change nothing; a stale sha256 is a conflict; comments survive; a symlinked user file
  is refused; a text that would not load is refused.
- **Guard and trust**: a repository that forbids a path is enforced under the shared server; the Settings form edits
  only its own pack and still refuses a protected change.
- **`test_dashboard_js.py`** (the W3 cases done in 3.16): the switcher renders from `/api/repos`, the selection survives a reload, an action on an
  inbox row posts to that row's repository, a one-repository page shows no switcher, and each Settings field shows its
  save target.
- **`test_portability.py`** must still pass: the engine names no project.

## Boundaries

- **Always:** key a repository by `pack.repo_id`; resolve a repository only from the server's map; keep `repo`, `cfg`
  and `config` together in one `RepoContext`; leave the single-repository routes and output byte-identical; give every
  new config key a Settings Form field (or a `_PROTECTED_KEYS` entry), `validate` checks, a `test_config_edit.py` case
  and a playbook §14 line; run `uvx ruff@0.17.4 check .` and `bin/agentflow validate` ("0 warnings") before calling a
  slice done; bump the version and the changelog in each PR.
- **Ask first:** the run cap and merged digest (W5); the login service; any widening of the user-level allow-list;
  any change to `pack.py` or `config_trust.py` (security overlay).
- **Never:** let a run read or write a repository other than its own; accept a path from an HTTP request; let the page
  write `hitl.notify`, `agents.*.user_settings` or `dashboard.host`; write `workspace.*` into a pack; run anything
  inside a scanned repository; add a third-party dependency; change the golden `explain` files.

## Rollout (each slice is one PR, feature lane on agentflow itself, each bumps the version: 3.12 → 3.13 → …)

| Slice | Scope | Depends on |
|---|---|---|
| **W1 — workspace settings, CLI** | the `workspace.*` and dashboard user keys, `load_user_settings`, `repo_set.py`, `-C`, `--all`, `workspace list`, the `validate` checks, playbook §10, §14, §17. The workspace is edited by hand in the user file until W4. *(security overlay: `layers.py`)* | none |
| **W2 — one server, many repositories** | `RepoContext`, the map and rescan, `/api/repos`, the `/api/repos/<id>/…` prefix, tagged SSE, staggered refresh, `dashboard` startable anywhere. The page still shows the default repository, so the legacy API and UI stay compatible. | W1 |
| **W3 — the page** | `repos.js` (switcher, badges, not-set-up group), cross-repository Inbox and Agents, the merged `/api/inbox` and `/api/agents` (each item with `repo`) and the page's switch to `/api/events?all=1`, the `core.js` prefix, the repository name in notification titles and the tab title. | W2 |
| **W4 — two-level Settings** | the User section, field routing with save-target badges, `/api/user-config` and its guarded write, the empty-state onboarding. *(security overlay: `config_edit.py` write path)* | W2 |
| **W5 — cap and digest (conditional)** | `workspace.max_active_runs` held with a lock under `user_state_root()`, and a merged digest; built only if real use shows the need. | W2 |

W1 is useful on its own (scripts, `-C`, a cross-repository `inbox` from any directory). W2 is a refactor whose
single-repository behaviour is already pinned by the existing tests. W3 and W4 are independent of each other.

## Success criteria

1. `agentflow -C <path> <cmd>` gives the same result as `cd <path> && agentflow <cmd>` for every command, including
   from inside a linked worktree.
2. With `workspace.roots` set to a folder of three repositories, one with a waiting question, `agentflow inbox --all`
   run from a directory that is no repository lists the question under its repository's heading, which gives the `-C`
   option that makes the suggested command run there. `status --all` and `agents --all` list every repository's runs. A broken pack in one repository is one
   `error:` line under its heading (for the commands that load the pack) and exit code 1.
3. `agentflow dashboard` run from outside any repository serves the three repositories from one port and one token. The
   page has a switcher with a waiting-items badge per repository. Switching repositories pauses nothing. Approving an
   item from the All-repositories Inbox changes only that item's run, in its own repository.
4. A request that names an unknown repository, or a run id that belongs to another repository, is a 404 and reads no
   file.
5. With no workspace configured, `agentflow dashboard` in a repository answers every legacy route and renders the page
   exactly as before, and the full existing test suite passes unchanged.
6. On first start with no workspace and no repository, the page opens on Settings → Workspace; adding a folder there
   writes only `workspace.roots` to the user file, and the repositories appear within a rescan with no restart.
7. A field that belongs to the user level saves to the user file and a project field saves to the selected repository's
   pack, each marked on the page. A request to write `hitl.notify`, `agents.*.user_settings` or `dashboard.host`
   through `/api/user-config` is refused and changes nothing.
8. One repository's failing refresh, pack or `gh` never stops another repository's refresh, reminders or runs; a slow
   one may delay the next within a round.
9. `ruff`, `validate` (0 warnings) and `test_portability.py` pass.

## Alternatives considered

- **One dashboard per repository on a port range, plus a launcher page.** No refactor, but it keeps four tokens, four
  tabs and no cross-repository Inbox. It is what developers do today.
- **A separate `workspace.yaml` registry with `workspace add|scan|remove`.** The first draft. Replaced: config keys in the
  user file plus the Settings page give one way to change the workspace and reuse the layers, and the file is not one
  more thing to document.
- **A workspace-level pack holding every repository's config.** Breaks the engine/pack split and couples repositories
  that should release separately.
- **A background daemon that owns every repository's runs.** One place for scheduling, but it makes the runner
  long-lived and stateful and turns a CLI into a service. Rejected: runs stay independent processes the dashboard
  spawns.
- **A merged Board.** Different lanes, phases and gates per pack would show the union of columns, mostly empty.
- **Per-request `?repo=<path>`.** Takes a path from the network.
- **A per-repository token in one process.** More prompts, and a page that holds all the tokens protects nothing more.
- **A login-time dashboard service now.** Deferred (Non-goals).

## Decisions (grilling, 2026-11-26)

1. **Dashboard scope.** A configured workspace is served by plain `agentflow dashboard`; with none configured it stays
   single-repository.
2. **No run cap yet.** W5, conditional.
3. **Digests stay per repository,** with the repository name in the notification title.
4. **One token and one port per user,** and the dashboard runs from anywhere, tied to no repository.
5. **The workspace is user-level config,** not a registry file; the pack never carries it.
6. **Two-level Settings.** Personal keys save at user level, everything else at the selected project.
7. **Boards stay per repository;** only Inbox and Agents span repositories.
8. **CLI writes stay single-repository,** the dashboard acts across repositories with the repository in each request.
9. **User-level editing is limited** to low-risk personal keys; `hitl.notify`, `agents.*.user_settings` and
   `dashboard.host` stay hand-edited.
10. **Dashboard process settings** (`port`, `board_refresh_s`, `done_shown`) are user keys, read through a no-pack loader.
11. **Empty start** opens Settings → Workspace.
12. **Every discovered repository stays live;** refreshes are staggered; the rescan runs every minute and on save.
13. **Repositories with no pack** are listed as "not set up" and never written to.
14. **`workspace list` is the only workspace subcommand.**
15. **No login service now;** `agentflow` as an installed tool is the "user-level install".

## Open Questions

Two small points I added while writing this down. Approved as drafted: `workspace.repos` stays, and a repository the
dashboard starts in that is outside the workspace is not added.

1. **`workspace.repos`** (explicit paths outside any root) was not asked in the grilling. It is included so a repository
   that does not sit under a root can join; drop it if `roots` alone is enough.
2. **The repository the dashboard starts in** is preselected when it is in the workspace. If it is *not* in the
   workspace, this draft does not add it. Say if it should join the session anyway.

## Next step

When this is approved (edit the status line from `Status: proposed` to `approved`), plan slice W1 with `writing-plans`
into `docs/plans/YYYY-MM-DD-agentflow-harness-workspace-w1.md`, with `security-and-hardening`, because W1 changes
`layers.py`, which triggers the security overlay.

## Before W2 starts

Found by the W1 whole-branch review; each must be settled in W2's plan, because W2 lets the dashboard start runs from
the resolved map.

- **A root's child may resolve outside the root.** A child whose `.git` is a file (a linked worktree) resolves to its
  main checkout, which can be anywhere on the machine, and then joins the workspace. That is harmless for read-only
  views. Before the dashboard starts runs from the map, decide whether a root's child may resolve to a checkout outside
  its root (recommendation: no, drop it with a `Skipped` line that says so).
  Settled in the W2 plan: such a child is dropped with a `Skipped` line that names its main checkout.
- **`UserSettings.dashboard` is unvalidated** beyond what `check_user` and `validate` already do. W2 reads `port`,
  `board_refresh_s` and `done_shown` from it and must range-check them as `validate` does.
  Settled in the W2 plan: the dashboard range-checks them (`info_numbers`, `port_of`); a bad port is an error that
  names `dashboard.port`, and a bad refresh or count becomes its default.
- **A git-refused root child is dropped silently** (a submodule, a `--separate-git-dir` repository). `workspace list`
  cannot say why a repository is missing; W2 or W3 should give it a `Skipped` line.
  Settled in the W2 plan: it is a `Skipped` line with its folder name, which `workspace list` and `--all` print.
