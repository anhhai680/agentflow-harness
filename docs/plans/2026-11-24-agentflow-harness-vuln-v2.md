# Vuln lane, slice V2 (the Backlog): Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** With `vuln.sync: true`, the dashboard shows each open Dependabot alert group (one package in one manifest,
at or above `vuln.min_severity`) as a Backlog card with a severity pill. Start on the card runs `vuln --alert N`.

**Architecture:** Pure alert grouping and cache re-checking go in `vuln.py`, next to `parse_alert`. `backlog.py` gains
`sync_alerts`, a sibling of `sync_issues`. Once per refresh it makes one `gh api` call for the alerts and one
`gh pr list` call for the Dependabot PRs, and caches the groups in `backlog_state.json`. `snapshot` builds `alert:<N>`
cards from that cache and never calls `gh`. `begin_start` learns the `alert` key, and `module_queue.spawn_run`
learns the `alert` param. `board.js` draws the pill and two new badges.

**Tech Stack:** Python 3.12, stdlib + `pyyaml`, `gh` (argv lists only, `GET` only), vanilla JS in
`agentflow/dashboard_ui/`, pytest with `tests/bin/gh`, and node for `test_dashboard_js.py`.

**Spec:** `docs/specs/agentflow-vuln-fix-proposal.md`: §Design 8 "Backlog sync", the Decided behaviour bullet on open
Dependabot PRs ("the V2 card shows 'Dependabot PR #N open'"), the last acceptance criterion, and §Rollout V2. V1
has shipped (engine 3.6). Read `backlog.py`'s issue sync (`sync_issues`, `_synced`, `_issue_blocker`, `snapshot`,
`begin_start`) first: this slice copies its shape.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No third-party import.
- No project strings in engine files: `test_portability.py` stays green.
- `agentflow/tests/fixtures/explain/*.txt` stay byte-identical.
- No new config key. `vuln.sync` and `vuln.min_severity` already ship with their Settings fields, `validate` checks,
  `test_config_edit` cases and playbook lines. Task 2 adds one `validate` check on `vuln.sync`, with its test.
- Alert text is data. A package name, a manifest path and advisory ids reach a card only after the `vuln.py`
  validators pass. Titles, summaries and descriptions never reach the board, an argv or a refusal message.
- `gh` is called with argv lists, `GET` only. The board never dismisses, comments on or closes an alert or a
  Dependabot PR.
- `snapshot` and `begin_start` never call `gh`. Only `sync_alerts` (from `refresh` or `refresh_once`) does.
- A `gh` failure keeps the last cached list and sets `alerts_note`. It never raises out of `sync_alerts`.
- One sync makes exactly one alerts call:
  `api repos/{owner}/{repo}/dependabot/alerts?state=open&severity=<floor..critical, comma-joined>&per_page=100`.
- Card key `alert:<N>`, where N is the lowest alert number in the group. Item `source` and `kind` are `"alert"`.
- **Deviation from the spec, flagged for review:** §8 says Start runs `vuln --alert <lowest number in the group>`.
  This plan starts the lowest number *that has a patched version*, because intake refuses an alert with no patched
  version. The card key stays the group's lowest number.
- `vuln.py` and `module_queue.spawn_run`'s argv are security-sensitive. Plan and review those changes with
  `security-and-hardening` and the `security-auditor` agent.
- `uvx ruff@0.17.4 check .` prints `All checks passed!` before every commit. `bin/agentflow validate` prints
  `0 warnings` on this repository's pack and on both fixture packs.

## Review Focus

1. **One hostile alert in the listing.** A package `x; rm -rf ~`, a manifest `../../etc/passwd` or a bad GHSA id in
   one entry must drop that entry and bump a "skipped" count. It must not empty the board or stop the sync, and it
   must never show. Pinned in Task 1 (`group_alerts`) and Task 2 (the note).
2. **A tampered `backlog_state.json`.** A cached group with a bad package, a non-https url or a string alert number
   is dropped at read time (`check_group`), as `_cached_refs` does for issues. Pinned in Task 2.
3. **The lowest alert has no fix, but a sibling has one.** The card is startable, and Start passes the fixable
   number. A group in which no alert has a fix is `no_fix`, and Start answers 409. Pinned in Task 3.
4. **A vuln run started from the CLI, not the board.** The card for that package and manifest shows `in_progress`,
   and Start answers 409. A run for the same package in another manifest doesn't count. A cancelled or failed run
   frees the card. Pinned in Task 3.
5. **`min_severity` raised between refreshes.** Cards under the new floor disappear on the next `snapshot`,
   without waiting for a `gh` call. Pinned in Task 2.

---

### Task 1: `vuln.py`: group an alert listing and re-check a cached group

**Files:**
- Modify: `agentflow/vuln.py`: add the functions below next to `siblings`, and turn `_dependabot_prs`'s
  title-matching into `prs_naming` so intake and sync share it.
- Test: `agentflow/tests/test_vuln.py`

**Interfaces:**
- Consumes: `parse_alert`, `Alert`, `severity_at_least`, `SEVERITIES`, `GHSA_ID`, `CVE_ID`, `_package`, `_manifest`,
  `_url`, `ECOSYSTEMS` (all existing, in `vuln.py`).
- Produces:
  - `ALERTS_MAX = 100`
  - `severity_query(floor: str) -> str`: the severities at or above `floor`, comma-joined (`"high,critical"`).
  - `group_alerts(listing: object, floor: str) -> tuple[list[dict], int]`: the groups, and how many entries were
    skipped as unreadable. A listing that is not a list raises `AlertError`.
  - `check_group(value: object) -> dict | None`: a cached group checked again as untrusted input. It returns
    `None` when any field fails.
  - `prs_naming(package: str, prs: object) -> list[int]`: the PR numbers whose title names `package` as a whole
    token. It takes the parsed `gh pr list` reply; `_dependabot_prs` now calls it.
  - A group is exactly this dict:
    `{"ecosystem": str, "package": str, "manifest": str, "severity": str, "alerts": list[int], "fixable": list[int], "ids": list[str], "url": str, "dependabot_prs": list[int]}`.
    `alerts` is ascending and holds every open alert of the group at or above the floor. `fixable` is the ascending
    subset with a patched version. `severity` is the highest severity in `alerts`. `ids` is the union of the ids,
    in alert order. `url` is the `_url`-checked `html_url` of `fixable[0]`, else of `alerts[0]`, else `""`.
    `dependabot_prs` is always `[]` here: sync fills it in.

- [ ] **Step 1: Write the failing tests** (reuse `_alert`, `SIBLING`, `OTHER_MANIFEST` in `test_vuln.py`)

```python
def test_severity_query_lists_the_floor_and_above():
    assert vuln.severity_query("low") == "low,medium,high,critical"
    assert vuln.severity_query("high") == "high,critical"

def test_group_alerts_groups_by_package_and_manifest():
    groups, skipped = vuln.group_alerts([_alert(42), SIBLING, OTHER_MANIFEST], "low")
    assert skipped == 0
    by_manifest = {g["manifest"]: g for g in groups}
    main = by_manifest[_alert(42)["dependency"]["manifest_path"]]
    assert main["alerts"] == [42, 43] and main["fixable"] == [42, 43]
    assert main["severity"] == _alert(42)["security_advisory"]["severity"]  # SIBLING is low
    assert main["ids"][0] == _alert(42)["security_advisory"]["ghsa_id"] and "GHSA-mmpq-2345-vwxj" in main["ids"]
    assert main["url"].endswith("/dependabot/42") and main["dependabot_prs"] == []
    assert by_manifest["web/requirements.txt"]["alerts"] == [44]

def test_group_alerts_drops_closed_and_below_floor():
    closed = _alert(45, state="fixed")
    groups, _ = vuln.group_alerts([_alert(42), SIBLING, closed], "medium")
    assert [g["alerts"] for g in groups] == [[42]]  # SIBLING is low, 45 is closed

def test_a_group_whose_lowest_alert_has_no_fix_still_has_a_fixable_one():
    nofix = _alert(40, **{"security_vulnerability.first_patched_version": None})
    groups, _ = vuln.group_alerts([nofix, _alert(42)], "low")
    assert groups[0]["alerts"] == [40, 42] and groups[0]["fixable"] == [42]
    assert groups[0]["url"].endswith("/dependabot/42")

def test_a_group_with_no_fix_has_no_fixable():
    groups, _ = vuln.group_alerts([_alert(40, **{"security_vulnerability.first_patched_version": None})], "low")
    assert groups[0]["fixable"] == []

@pytest.mark.parametrize("change", [
    {"dependency.package.name": "x; rm -rf ~"}, {"dependency.manifest_path": "../../etc/passwd"},
    {"security_advisory.ghsa_id": "GHSA-bad"}, {"number": "42"}])
def test_one_bad_entry_is_skipped_and_counted(change):
    groups, skipped = vuln.group_alerts([_alert(41, **change), _alert(42), "junk"], "low")
    assert skipped == 2 and [g["alerts"] for g in groups] == [[42]]

def test_group_alerts_refuses_a_non_list():
    with pytest.raises(vuln.AlertError):
        vuln.group_alerts({"message": "x"}, "low")

def test_check_group_round_trips_and_refuses_tampering():
    group = vuln.group_alerts([_alert(42)], "low")[0][0]
    assert vuln.check_group(group) == group
    for key, bad in [("package", "a b"), ("manifest", "/etc/passwd"), ("severity", "urgent"),
                     ("alerts", ["42"]), ("alerts", []), ("fixable", [99]), ("ids", ["not-an-id"]),
                     ("url", "javascript:alert(1)"), ("dependabot_prs", [True]), ("ecosystem", "cobol")]:
        assert vuln.check_group({**group, key: bad}) is None, key
    assert vuln.check_group({**group, "extra": 1}) is None and vuln.check_group("x") is None

def test_prs_naming_matches_whole_tokens_only():
    prs = [{"number": 7, "title": "Bump requests from 2.31.0 to 2.32.0"},
           {"number": 8, "title": "Bump requests-toolbelt from 1.0 to 1.1"}, {"number": True, "title": "requests"}]
    assert vuln.prs_naming("requests", prs) == [7]
    assert vuln.prs_naming("requests", "junk") == []
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_vuln.py -k "severity_query or group or prs_naming or bad_entry" --import-mode=prepend -q`
Expected: FAIL with `AttributeError: module 'agentflow.vuln' has no attribute ...`

- [ ] **Step 3: Implement the five names in `agentflow/vuln.py`**

`group_alerts` calls `parse_alert` on each entry, and it counts both an `AlertError` and a non-dict as skipped. It
keeps only `state == "open"` alerts at or above `floor`, groups them by `(ecosystem, package, manifest)`, and keeps
first-seen group order. `check_group` wants exactly the nine keys. It reuses `_package`, `_manifest`, the id
patterns and `_url`, and it checks that `fixable` is a subset of `alerts` and `alerts` is non-empty and ascending.
`_dependabot_prs` keeps its signature and its `gh` call, and returns `prs_naming(package, data)`.

- [ ] **Step 4: Run `test_vuln.py` in full and see it pass** (the intake PR-warning tests still pass after the refactor)

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_vuln.py --import-mode=prepend -q`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/vuln.py agentflow/tests/test_vuln.py
git commit -m "Group Dependabot alerts by package and manifest and re-check a cached group."
```

---

### Task 2: `backlog.sync_alerts` and the refresh

**Files:**
- Modify: `agentflow/backlog.py`: `sync_alerts`, `_synced_alerts`, a call in `refresh`, and the module docstring
  (add the fourth kind of item).
- Modify: `agentflow/dashboard.py:369-378` (`refresh_once`): with no `dashboard.backlog` block, also call
  `sync_alerts` when `_alerts_on(cfg)`.
- Modify: `agentflow/config.py:2881` (`Validator.vuln`): one new check.
- Modify: `agentflow/tests/bin/gh:98-106`: the listing also filters on `severity`, a comma list matched against
  `security_advisory.severity`.
- Test: `agentflow/tests/test_backlog.py` (a new section, `# --- synced Dependabot alerts`), `agentflow/tests/test_pack.py`
  (the validate case), `agentflow/tests/test_dashboard.py` (`refresh_once`).

**Interfaces:**
- Consumes: Task 1's `group_alerts`, `check_group`, `prs_naming`, `severity_query`, `ALERTS_MAX`, and
  `vuln.ALERTS_API`, `vuln.severity_at_least`.
- Produces:
  - `_alerts_on(cfg: Config) -> bool`: `cfg.vuln.sync and cfg.vuln.source == "dependabot" and "vuln" in cfg.lanes`.
  - `sync_alerts(cfg: Config, repo: Path) -> str`: the note the board shows (`""` when all is well). It never raises.
  - `_synced_alerts(cfg: Config, state: dict) -> list[dict]`: the cached groups that pass `check_group` and are
    at or above the *current* `vuln.min_severity`. It returns `[]` when `_alerts_on` is false.
  - State keys in `backlog_state.json`: `alerts` (the list of groups), `alerts_note`, `alerts_at`.
  - `refresh(...)` returns an extra key, `alerts_note`.
- Calls `sync_alerts` makes, in this order:
  1. `["api", f"{vuln.ALERTS_API}?state=open&severity={severity_query(floor)}&per_page=100"]`.
  2. `["pr", "list", "--author", "app/dependabot", "--state", "open", "--json", "number,title", "--limit", "100"]`.
- Notes, joined with `"; "`:
  - when the alerts call fails: `f"Dependabot alerts not updated: {_reason(exc)}"`, and the last list stays;
  - when the PR call fails: `"Dependabot pull requests not read"`, with every `dependabot_prs` set to `[]`;
  - when entries were skipped: `f"{n} alert(s) could not be read and are not shown"`;
  - when `len(listing) >= ALERTS_MAX`: `f"only the first {ALERTS_MAX} open alerts are shown"`.

- [ ] **Step 1: Write the failing tests**

Add a fixture `abk(bk, gh_alerts)`. `gh_alerts` (conftest) already adds the `vuln` lane, sets `vuln.probe: ok`,
commits `requirements.txt` and serves `$FAKE_GH_ALERTS`. `FlowRepo.configure` replaces the whole patch on each
call, so add a helper `_vuln(board, **keys)` that calls
`board.fr.configure(patch={"vuln": {"probe": "ok", "sync": True, **keys}}, edit=add_vuln_lane)`. `abk` calls
`_vuln(bk)` and sets `bk.alerts = gh_alerts`. Use `abk.alerts.write([...])` to set the listing, with `_alert`, `SIBLING` and
`OTHER_MANIFEST` from `test_vuln`. A helper `_cached(abk)` returns
`backlog._synced_alerts(abk.cfg, backlog._load_state(abk.root))`. Task 2 tests read the cache, never cards.

```python
def test_alert_sync_is_off_by_default_and_calls_no_gh(bk, gh_alerts):
    assert bk.refresh()["alerts_note"] == ""  # gh_alerts adds the lane, but vuln.sync stays false
    assert not [c for c in _gh_calls(bk) if c[:1] == ["api"]]
    assert _cached(bk) == []

def test_a_refresh_makes_one_alerts_call_and_one_pr_call(abk, monkeypatch):
    abk.alerts.write([_alert(42), SIBLING])
    abk.refresh()
    calls = _gh_calls(abk)
    assert [c for c in calls if c[:1] == ["api"]] == [
        ["api", "repos/{owner}/{repo}/dependabot/alerts?state=open&severity=low,medium,high,critical&per_page=100"]]
    assert ["pr", "list", "--author", "app/dependabot", "--state", "open", "--json", "number,title",
            "--limit", "100"] in calls

def test_the_severity_floor_goes_to_gh_and_is_applied_again_on_read(abk, monkeypatch):
    abk.alerts.write([_alert(42), SIBLING])  # 42 is high, SIBLING low: one group
    abk.refresh()
    assert [g["alerts"] for g in _cached(abk)] == [[42, 43]]
    _vuln(abk, min_severity="critical")  # raised after the sync: no gh call needed
    assert _cached(abk) == []

def test_a_failed_alerts_call_keeps_the_last_list_and_says_why(abk, monkeypatch):
    abk.alerts.write([_alert(42)])
    abk.refresh()
    monkeypatch.setenv("FAKE_GH_ALERTS_STATUS", "403")
    assert abk.refresh()["alerts_note"].startswith("Dependabot alerts not updated:")
    assert [g["alerts"] for g in _cached(abk)] == [[42]]

def test_a_failed_pr_list_drops_only_the_pr_lines(abk, monkeypatch):
    abk.alerts.write([_alert(42)])
    monkeypatch.setenv("FAKE_GH_FAIL_ON", "pr list")
    assert "Dependabot pull requests not read" in abk.refresh()["alerts_note"]
    assert _cached(abk)[0]["dependabot_prs"] == []

def test_an_unreadable_alert_is_counted_not_shown(abk, monkeypatch):
    abk.alerts.write([_alert(41, **{"dependency.package.name": "x; rm -rf ~"}), _alert(42)])
    assert "1 alert(s) could not be read" in abk.refresh()["alerts_note"]
    assert [g["alerts"] for g in _cached(abk)] == [[42]]

def test_a_tampered_cache_group_is_dropped(abk, monkeypatch):
    abk.alerts.write([_alert(42)])
    abk.refresh()
    state = backlog._load_state(abk.root)
    backlog._save_state(abk.root, alerts=[{**state["alerts"][0], "package": "$(id)"}])
    assert _cached(abk) == []

def test_turning_sync_off_clears_the_cache(abk, monkeypatch):
    abk.alerts.write([_alert(42)])
    abk.refresh()
    _vuln(abk, sync=False)
    abk.refresh()
    assert backlog._load_state(abk.root).get("alerts") == []
```

In `test_pack.py`: `vuln.sync: true` with `vuln.source: ""`, with `vuln` not in `lanes`, or with `vuln.probe: ""`
each gives one `validate` error at `vuln.sync`. Its message names what is missing.

In `test_dashboard.py`: a pack with no `dashboard.backlog` block and `vuln.sync: true` has `refresh_once` call
`sync_alerts`. Monkeypatch it with a recorder.

- [ ] **Step 2: Run them and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_backlog.py -k alert --import-mode=prepend -q`
Expected: FAIL (`KeyError: 'alerts_note'`, and `AttributeError` for `_synced_alerts`).

- [ ] **Step 3: Implement `_alerts_on`, `sync_alerts` and `_synced_alerts` in `backlog.py`, the `refresh_once` call, the validate check and the fake-gh `severity` filter**

`sync_alerts` mirrors `sync_issues`. When `_alerts_on` is false and the state still holds a non-empty `alerts`, it
saves `alerts=[], alerts_note=""`. It catches `(GhError, OSError, ValueError, TypeError, RecursionError)` around
the alerts call and around `group_alerts`. `vuln.AlertError` is a `ValueError`. The PR call has its own `try`,
and its result is matched per group with `prs_naming`. `refresh` calls it right after `sync_issues`.

- [ ] **Step 4: Run them and see them pass**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_backlog.py agentflow/tests/test_pack.py agentflow/tests/test_dashboard.py --import-mode=prepend -q`
Expected: all pass.

- [ ] **Step 5: Lint, validate and commit**

```bash
uvx ruff@0.17.4 check . && bin/agentflow validate
git add agentflow/backlog.py agentflow/dashboard.py agentflow/config.py agentflow/tests/bin/gh agentflow/tests/test_backlog.py agentflow/tests/test_pack.py agentflow/tests/test_dashboard.py
git commit -m "Sync open Dependabot alert groups into the Backlog state once per refresh."
```

---

### Task 3: Alert cards, Start and the run they spawn

**Files:**
- Modify: `agentflow/backlog.py`:
  - `Item.key` (source `"alert"` gives `alert:<params.alert>`), `Backlog.alert_overlay`;
  - `_picture` (adds `pic["alerts"]` and `pic["alert_runs"]`), `snapshot` (alert cards and `alerts_note`);
  - `parse_key` (the `alert` kind, through `vuln.alert_number`), `begin_start` (the `alert` branch);
  - `_live_clash` (adds `"alert"` to the names it compares).
- Modify: `agentflow/module_queue.py:1102` and `:1124`: add `"alert"` to `SPAWN_PARAMS`, and refuse a value that
  is not all digits (`StartFailed`).
- Modify: `agentflow/dashboard.py`: the `/api/backlog/remove` branch refuses `alert:` keys, and the module
  docstring at line 37 names `alert:<number>`.
- Test: `agentflow/tests/test_backlog.py`, `agentflow/tests/test_next_queue.py` (spawn argv),
  `agentflow/tests/test_dashboard.py` (409 on Start and on Remove).

**Interfaces:**
- Consumes: Task 2's `_synced_alerts`. Also `vuln.read_alert`, `vuln.AlertError`, `vuln.alert_number`,
  `module_queue.issue_run_holds`.
- Produces:
  - `Backlog.alert_overlay(number: str) -> Item | None`: the newest record with `source == "alert"` and
    `params["alert"] == number`.
  - `_alert_holder(group: dict, runs: list[Run]) -> Run | None`: the run with `pipeline == "vuln"`, for which
    `issue_run_holds` is true, whose `alert.json` has the group's `(ecosystem, package, manifest)`. A run whose
    `alert.json` can't be read (`AlertError`) holds nothing.
  - An alert card is `make(...)`'s dict with `kind="alert"`, `id=str(alerts[0])`, `lane="vuln"` and
    `label=f"{package} ({ecosystem})"`, plus `severity`, `manifest`, `alerts`, `ids`, `dependabot_prs` and `url`.
  - Statuses, in this order: `starting` (the overlay is starting), `in_progress` (there is a holder; `note` is
    `f"run {run.id} holds it"`), `no_fix` (`fixable == []`; `note` is `"no patched version yet"`), else `ready`.
  - `snapshot()` returns an extra key, `alerts_note`, which is `""` when `_alerts_on` is false.
  - `begin_start(cfg, repo, "alert:<N>")` returns `StartJob(lane="vuln", params={"alert": str(fixable[0])}, ...)`.
    The overlay item it appends has `source="alert"`, `kind="alert"`, `params={"alert": str(N)}` and
    `label=f"alert #{N}"`.
  - `Refused` messages for `alert:<N>`:
    - `"alert sync is off"` when `_alerts_on` is false;
    - `f"alert #{N} is not an open alert group at or above {floor} as of the last refresh"`;
    - `f"alert #{N}: no patched version yet"`;
    - `f"alert #{N}: run {id} holds {package} in {manifest}"`;
    - the shared `f"{key} is already starting"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_an_alert_card_shows_the_group(abk, monkeypatch):
    monkeypatch.setenv("FAKE_GH_PR_LIST", json.dumps([{"number": 7, "title": "Bump requests from 2.31.0 to 2.32.0"}]))
    abk.alerts.write([_alert(42), SIBLING])
    abk.refresh()
    card = abk.cards()["alert:42"]
    assert (card["kind"], card["lane"], card["status"], card["label"]) == ("alert", "vuln", "ready", "requests (pip)")
    assert card["alerts"] == [42, 43] and card["dependabot_prs"] == [7] and card["removable"] is False
    assert card["first_phase"] == lane_first_phase(abk.cfg, "vuln")

def test_start_passes_the_lowest_fixable_alert(abk, monkeypatch):
    abk.alerts.write([_alert(40, **{"security_vulnerability.first_patched_version": None}), _alert(42)])
    abk.refresh()
    job = backlog.begin_start(abk.cfg, abk.root, "alert:40")
    assert (job.lane, job.params) == ("vuln", {"alert": "42"})
    assert abk.cards()["alert:40"]["status"] == "starting"

def test_a_group_with_no_fix_shows_and_refuses_start(abk, monkeypatch):
    abk.alerts.write([_alert(40, **{"security_vulnerability.first_patched_version": None})])
    abk.refresh()
    assert abk.cards()["alert:40"]["status"] == "no_fix"
    with pytest.raises(backlog.Refused, match="no patched version yet"):
        backlog.begin_start(abk.cfg, abk.root, "alert:40")

def test_a_cli_run_for_the_package_holds_the_card_and_a_cancel_frees_it(abk, monkeypatch):
    abk.alerts.write([_alert(42), OTHER_MANIFEST])
    abk.refresh()
    run = test_vuln._start(abk.fr)  # a real `run vuln --alert 42` intake: it writes alert.json
    cards = abk.cards()
    assert cards["alert:42"]["status"] == "in_progress" and run.id in cards["alert:42"]["note"]
    assert cards["alert:44"]["status"] == "ready"  # same package, other manifest
    with pytest.raises(backlog.Refused, match="holds requests"):
        backlog.begin_start(abk.cfg, abk.root, "alert:42")
    run_control.cancel(run, "test")
    assert abk.cards()["alert:42"]["status"] == "ready"

def test_an_alert_start_that_fails_keeps_its_reason(abk, monkeypatch):
    abk.alerts.write([_alert(42)])
    abk.refresh()
    abk.spawn.fail = "Dependabot alert #42 is fixed: only an open alert starts a vuln run"  # `Spawner.fail`
    abk.start("alert:42")
    card = abk.cards()["alert:42"]
    assert card["status"] == "ready" and card["failure"].startswith("Dependabot alert #42 is fixed")

@pytest.mark.parametrize("key", ["alert:x", "alert:-1", "alert:"])
def test_a_bad_alert_key_is_invalid(key):
    with pytest.raises(backlog.Invalid):
        backlog.parse_key(key)
```

In `test_next_queue.py`: `spawn_run(..., "vuln", {"alert": "42"})` puts `--alert=42` in the argv. `{"alert": "4 2"}`
and `{"alert": "--x"}` raise `StartFailed`.

In `test_dashboard.py`: `POST /api/backlog/start {"item": "alert:40"}` on a `no_fix` group answers 409, and
`POST /api/backlog/remove {"item": "alert:42"}` answers 409.

Use the existing `Board` helpers (`start`, `spawn`: a `Spawner`, whose `fail` makes it raise `StartFailed`).

- [ ] **Step 2: Run them and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_backlog.py agentflow/tests/test_next_queue.py -k "alert or spawn" --import-mode=prepend -q`
Expected: FAIL (`Invalid` from `parse_key`, no `alert:` cards, and `StartFailed("'alert' cannot be set from here")`).

- [ ] **Step 3: Implement the changes listed under Files**

`_picture` builds `pic["alerts"]` from `_synced_alerts`. For each group, `pic["alert_runs"]` holds its
`_alert_holder`, keyed by `alerts[0]`. Alert cards stay out of `module_queue.dependency_view` and out of
`after_choices`. `run_start` needs no change: intake re-asks GitHub, and its refusal becomes `item_failed`.

- [ ] **Step 4: Run the touched files and see them pass, the Task 2 card assertions included**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_backlog.py agentflow/tests/test_next_queue.py agentflow/tests/test_dashboard.py agentflow/tests/test_vuln.py --import-mode=prepend -q`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/backlog.py agentflow/module_queue.py agentflow/dashboard.py agentflow/tests/test_backlog.py agentflow/tests/test_next_queue.py agentflow/tests/test_dashboard.py
git commit -m "Show Dependabot alert groups as Backlog cards that start a vuln run, in progress while a run holds them."
```

---

### Task 4: The board: severity pill, badges, alert lines and the alerts note

**Files:**
- Modify: `agentflow/dashboard_ui/board.js`:
  - `backlogMark`: `no_fix` gives a `badge lock` "No fix yet", and `in_progress` gives a `badge` "In progress";
  - new `severityClass`, `alertLines`, and the alert parts of `backlogNode`;
  - `issueNode(url)` becomes `linkNode(url, text)`;
  - the `alerts-note` line next to `issues-note`.
- Modify: `agentflow/dashboard.html`:
  - CSS `.pill.sev-critical, .pill.sev-high` in `var(--bad)`, `.pill.sev-medium` in `var(--warn)`, `.pill.sev-low` in
    `var(--muted)`;
  - `.card.backlog.no_fix, .card.backlog.in_progress` take the muted left border;
  - `<div id="alerts-note" class="muted" role="status" hidden>` beside `#issues-note`, with the shared
    `margin-bottom` rule.
- Test: `agentflow/tests/test_dashboard_js.py`

**Interfaces:**
- Consumes: Task 3's card fields (`kind`, `severity`, `manifest`, `alerts`, `dependabot_prs`, `url`, `status`) and
  `snapshot().alerts_note`.
- Produces (pure, testable with `js(["board.js"], ...)`):
  - `severityClass(sev) -> string | null`: `"sev-" + sev` for `low|medium|high|critical`, else `null`. An unknown
    severity draws no pill.
  - `alertLines(item) -> string[]`: `[`${n} alert${n === 1 ? "" : "s"} · ${item.manifest}`]`, then one
    `"Dependabot PR #" + k + " open"` for each `k` in `item.dependabot_prs`.
- `backlogNode` for `kind === "alert"`:
  - the title, then the pill (`el("span", {class: "pill " + cls, text: item.severity})`), then `backlogMark`;
  - each `alertLines` entry as a `div.sub` set with `text:` (never as HTML);
  - `linkNode(item.url, "Alert ↗")`.
  Issues keep `linkNode(item.url, "Issue ↗")`.

- [ ] **Step 1: Write the failing tests**

```python
def test_severity_class_knows_the_four_and_nothing_else():
    got = js(["board.js"], "['low','medium','high','critical','urgent','<b>',null].map(severityClass)", {})
    assert got == ["sev-low", "sev-medium", "sev-high", "sev-critical", None, None, None]

def test_alert_lines_count_and_name_dependabot_prs():
    item = {"alerts": [42, 43], "manifest": "requirements.txt", "dependabot_prs": [7]}
    assert js(["board.js"], "alertLines(args)", item) == ["2 alerts · requirements.txt", "Dependabot PR #7 open"]
    assert js(["board.js"], "alertLines(args)", {**item, "alerts": [42], "dependabot_prs": []}) == [
        "1 alert · requirements.txt"]

def test_backlog_mark_names_the_alert_statuses():
    assert js(["board.js"], "backlogMark({status: 'no_fix'}).textContent", {}) == "No fix yet"
    assert js(["board.js"], "backlogMark({status: 'in_progress'}).textContent", {}) == "In progress"
```

If `_dom_stub.js`'s `el` doesn't expose `textContent` for `text:`, assert on the property it does set. Read the
stub first.

- [ ] **Step 2: Run them and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard_js.py -k "severity or alert_lines or backlog_mark" --import-mode=prepend -q`
Expected: FAIL (`ReferenceError: severityClass is not defined`).

- [ ] **Step 3: Implement the board.js and dashboard.html changes listed under Files**

- [ ] **Step 4: Run them and see them pass, with the rest of the dashboard tests**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard_js.py agentflow/tests/test_dashboard.py agentflow/tests/test_portability.py --import-mode=prepend -q`
Expected: all pass.

- [ ] **Step 5: See it on a real board**

Point `FAKE_GH_ALERTS` at a fixture, or run against a repository that has alerts, run `bin/agentflow dashboard`,
and check three things: an alert card with its pill, a `no_fix` card with no Start button, and the alerts note when
`gh` fails. If you can't open a browser, say so in the handoff.

- [ ] **Step 6: Lint and commit**

```bash
uvx ruff@0.17.4 check .
git add agentflow/dashboard_ui/board.js agentflow/dashboard.html agentflow/tests/test_dashboard_js.py
git commit -m "Draw a severity pill, alert lines and the no-fix and in-progress badges on Backlog alert cards."
```

---

### Task 5: Docs, changelog, version, full verification

**Files:**
- Modify: `docs/agentflow-playbook.md`:
  - the Backlog paragraph at line 294: a sibling paragraph for alert cards (grouping, severity floor, the pill,
    `No fix yet`, `In progress`, Dependabot PR lines, the note on a `gh` failure, not removable by hand, and Start
    running `vuln --alert <lowest fixable>`);
  - §13 "Patching a security alert": one sentence on `vuln.sync`;
  - §14 `vuln.sync`: the new `validate` rule (it needs `vuln.source: dependabot`, the `vuln` lane and a
    `vuln.probe`).
- Modify: `CLAUDE.md`: in the Safety and the UI bullet on `backlog.py`, name the alert sync. In the `vuln.py` bullet,
  name `group_alerts` and `check_group`.
- Modify: `docs/specs/agentflow-vuln-fix-proposal.md` §8: record the lowest-*fixable* Start rule and the extra
  `gh pr list` call per refresh, as a dated note. The spec is approved, so ask the user before you edit it.
- Modify: `CHANGELOG.md`, `pyproject.toml`, `agentflow/pack.py` (`ENGINE_VERSION` `3.6` → `3.7`, or past whatever
  `main` holds).

- [ ] **Step 1: Write the docs and the changelog `Added` lines under `## [Unreleased]`**

- [ ] **Step 2: Bump the version (CLAUDE.md "Every new PR bumps the version") and run the packaging test**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_packaging.py --import-mode=prepend -q`
Expected: pass.

- [ ] **Step 3: Full verification**

```bash
bin/agentflow validate                     # 0 warnings
uvx ruff@0.17.4 check .                    # All checks passed!
git diff --stat main -- agentflow/tests/fixtures/explain   # empty
uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q
```
Expected: `0 warnings`, `All checks passed!`, an empty diff, and the full suite green. Run `validate` on both
fixture packs as V1's Task 8 did.

- [ ] **Step 4: Commit**

```bash
git add docs/agentflow-playbook.md CLAUDE.md CHANGELOG.md pyproject.toml agentflow/pack.py
git commit -m "Document Dependabot alert cards on the Backlog and release engine 3.7."
```
