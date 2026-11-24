# Vuln lane, slice V3 (beyond Dependabot): Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**
- `agentflow run vuln --advisory GHSA-…|CVE-… --package NAME --manifest PATH` patches a known advisory in a
  repository without Dependabot alerts.
- With `vuln.dependency_review: true`, a vuln run doesn't mark its PR ready while the pushed head adds a dependency
  with an advisory at or above `vuln.min_severity`.

**Architecture:**
- **`vuln.py`:**
  - pure parsing of GitHub's global advisory reply and of the dependency-graph compare reply, next to `parse_alert`;
  - `gather_advisory`, a sibling of `gather_alert` that writes the same `alert.json` shape. The probe, conformance,
    signals and Backlog holds therefore need no change.
- **`ci_lane.CILane.run_ci`:** calls `vuln.review_dependencies` once CI passes and before `_ready`. A hit, or an
  unreadable reply, is a stop.
- **`doctor.py`:** one more read-only `gh api` call, made when the key is on.

**Tech Stack:** Python 3.12, stdlib + `pyyaml`, `gh` (argv lists only, `GET` only), pytest with `tests/bin/gh` and
`fake_agent.py`.

**Spec:** `docs/specs/agentflow-vuln-fix-proposal.md`, §Design 10 "V3: beyond Dependabot" (it records this slice's
decisions, and it is marked `proposed` until a human approves it) and §Rollout V3. V1 and V2 have shipped (engine
3.7). Read `vuln.gather_alert`, `vuln.pr_lines`, `intake.gather`, `pipeline.start` (the `--alert` checks) and
`ci_lane.CILane.run_ci` first: this slice copies their shape.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No third-party import.
- No project strings in engine files: `test_portability.py` stays green.
- `agentflow/tests/fixtures/explain/*.txt` stay byte-identical.
- No engine prompt changes, so no `pack_fix.Retired` entry. `alert.md` keeps its name for an `--advisory` run.
- Advisory text is data. The package, ecosystem, manifest, versions, ids and URL reach `alert.json`, a stop message
  or the PR only after `vuln.py`'s validators (`_package`, `_manifest`, `_version`, `_RANGE`, `GHSA_ID`, `CVE_ID`,
  `_url`) pass, and every PR or stop line passes `hitl.clean_fact`. Summaries and descriptions go only to `alert.md`,
  under `intake.DATA_NOTE`.
- `gh` gets argv lists and makes `GET` calls only:
  - `api advisories/<GHSA>`;
  - `api advisories?cve_id=<CVE>&per_page=100`;
  - `api repos/{owner}/{repo}/dependency-graph/compare/<base>...<head>`, where both are 40-hex shas checked
    before the call.
- The run never dismisses an alert and never merges.
- New config key `vuln.dependency_review` (bool, default `false`, **protected**). It ships with all four obligations:
  a `_PROTECTED_KEYS` entry, `validate` checks, a `test_config_edit` case and a playbook §14 line.
- New stop kinds `new_advisory` and `dependency_review_failed`, each with a way-on text in `hitl.py`.
- **Defaults taken for two unanswered planning questions, flagged for review:**
  - The dependency review check **fails closed**: a hit or an API error stops the run, and only `approve` gets past it.
  - It covers the **vuln lane only**.
- `vuln.py`, `intake.py` and `doctor.py` are security-overlay paths. Plan and review this work with
  `security-and-hardening` and the `security-auditor` agent.
- `uvx ruff@0.17.4 check .` prints `All checks passed!` before every commit. `bin/agentflow validate` prints
  `0 warnings` on this repository's pack and on both fixture packs.

## Review Focus

1. **An empty compare right after a push.** GitHub's dependency graph is built after a push, so the compare can
   come back `[]` while it lags. An empty list, or one with no change to the run's package, must be
   `dependency_review_failed`, never a clean pass. Pinned in Task 1 (`review_changes`) and Task 3.
2. **A CVE that maps to several GHSA advisories.** One may be withdrawn, and one may not name the package. The run
   takes the union of ids from the matches that aren't withdrawn and do name it, and nothing else. Pinned in Task 1.
3. **A package name in the advisory spelled another way.** `--package PyYAML` against `pyyaml`, or `Foo_Bar` against
   `foo-bar` for pip, must match. `--package Lodash` against npm `lodash` must not. Pinned in Task 1
   (`same_package`).
4. **An approved `new_advisory`, then a CI fix round pushes a new head.** The new sha is checked again. The earlier
   acceptance covers only the sha it was given for. Pinned in Task 3.
5. **A hostile compare reply.** A package name holding a newline or `## heading`, or a ghsa id that isn't one, makes
   the reply malformed (`dependency_review_failed`). Its text never reaches the stop or the PR. Pinned in Task 1 and
   Task 3.

---

### Task 1: `vuln.py`: parse a global advisory, match a package, and judge a dependency-graph compare

**Files:**
- Modify: `agentflow/vuln.py`: add the code below. The advisory model and matching go next to `parse_alert`, and
  `review_changes` goes next to `judge`.
- Modify: `agentflow/tests/conftest.py`: add `GLOBAL_ADVISORY: dict[str, Any]` next to `VULN_ALERT` (Task 2's
  tests use it too).
- Test: `agentflow/tests/test_vuln.py`

**Interfaces:**
- Consumes: `GHSA_ID`, `CVE_ID`, `SEVERITIES`, `ECOSYSTEMS`, `AlertError`, `_obj`, `_text`, `_package`, `_manifest`, `_version`,
  `_RANGE`, `_url`, `_references`, `MAX_TEXT`, `severity_at_least` (all existing).
- Produces:
  - `@dataclass(frozen=True) class Vulnerability`, with the fields `ecosystem: str`, `package: str`,
    `vulnerable_range: str` and `patched: str | None`.
  - `@dataclass(frozen=True) class Advisory`, with the fields:
    - `ghsa: str` and `cve: str | None`;
    - `severity: str`, one of `SEVERITIES` or `"unknown"`;
    - `withdrawn: bool`;
    - `url: str`, already `_url`-checked, else `""`;
    - `summary: str`, `description: str` and `references: tuple[str, ...]`;
    - `vulnerabilities: tuple[Vulnerability, ...]`.
  - `advisory_id(ref: object) -> str`: a stripped GHSA or CVE id. Anything else raises `ValueError` naming
    `--advisory`, with the input cut to 80 characters.
  - `parse_advisory(data: object) -> Advisory`. It raises `AlertError` for a bad `ghsa_id`, a `cve_id` that is
    neither null nor a CVE, or a severity outside `SEVERITIES + ("unknown",)`. A `vulnerabilities[]` entry is dropped
    silently, never refused, when:
    - its ecosystem isn't in `ECOSYSTEMS`;
    - its name fails `_package`;
    - its range fails `_RANGE`;
    - its `first_patched_version` is neither null nor a valid `_version`.

    `first_patched_version` is a plain string here, not `{identifier}`.
  - `same_package(ecosystem: str, a: str, b: str) -> bool`:
    - `pip`: PEP 503, `re.sub(r"[-_.]+", "-", name).lower()`;
    - `nuget` and `composer`: `casefold`;
    - otherwise: exact.
  - `match_package(advisories: Sequence[Advisory], package: str) -> tuple[list[Advisory], list[Vulnerability]]`:
    the advisories that aren't withdrawn and name `package`, and those vulnerabilities, in input order.
  - `normalize_severity(value: object) -> str | None`: `moderate` becomes `medium`, a `SEVERITIES` member stays
    itself, and anything else is `None`.
  - `review_changes(changes: object, floor: str, package: str, ecosystem: str) -> list[dict]`. It returns
    `{"ghsa", "severity", "package", "version", "manifest"}` for each vulnerability of a `change_type: "added"`
    entry whose severity normalizes to at or above `floor`. An unknown severity counts as a hit and is shown as
    `"unknown"`. It raises `AlertError` when:
    - the reply isn't a list;
    - an item isn't a dict;
    - `vulnerabilities` isn't a list;
    - an `advisory_ghsa_id` fails `GHSA_ID`;
    - a counted item's `name`, `version` or `manifest` fails `_package`, `_version` or `_manifest`;
    - no item (added or removed) names `package` under `same_package(ecosystem, …)`. The message says "the
      dependency graph shows no change to <package> yet".

- [ ] **Step 1: Write the failing tests** in `test_vuln.py`. Put `GLOBAL_ADVISORY` in `conftest.py`:
  `ghsa_id` `GHSA-cfgh-2345-vwxj`, `cve_id` `CVE-2024-1234`, severity `high`, `withdrawn_at` null,
  `html_url` `https://github.com/advisories/GHSA-cfgh-2345-vwxj`, and one vulnerability
  `{"package": {"ecosystem": "pip", "name": "requests"}, "vulnerable_version_range": "< 2.32.0",
  "first_patched_version": "2.32.0"}`:
  - `test_parse_advisory_keeps_validated_fields`: `.ghsa == "GHSA-cfgh-2345-vwxj"`, `.cve == "CVE-2024-1234"` and
    `.vulnerabilities[0] == Vulnerability("pip", "requests", "< 2.32.0", "2.32.0")`.
  - `test_parse_advisory_drops_bad_vulnerabilities_not_the_advisory`: adding entries with ecosystem `erlang`,
    package `x; rm -rf ~` and range `` `id` `` leaves exactly the one good entry.
  - `test_parse_advisory_refuses_bad_ids_and_severity` (parametrized): `ghsa_id: "GHSA-0000-0000-0000"`,
    `cve_id: "CVE-1"` and `severity: "severe"` each raise `AlertError`.
  - `test_advisory_id`: `" GHSA-cfgh-2345-vwxj "` and `"CVE-2024-1234"` pass, and `"42"` and
    `"GHSA-x; ls"` raise `ValueError` with `--advisory` in the message.
  - `test_same_package` (parametrized):
    - `("pip", "PyYAML", "pyyaml")`, `("pip", "Foo_Bar", "foo-bar")` and `("nuget", "Newtonsoft.Json",
      "newtonsoft.json")` are True;
    - `("npm", "Lodash", "lodash")` and `("pip", "requests", "requests-oauth")` are False.
  - `test_match_package_unions_live_matches` (Review Focus 2): with three advisories (A names requests; B names
    requests but has `withdrawn_at` set; C names only `urllib3`), it returns `[A]` and A's vulnerability.
  - `test_normalize_severity`: `moderate` → `medium`, `high` → `high`, and `"severe"` and `None` → `None`.
  - `test_review_changes_counts_added_at_or_above_floor`: the reply is
    - a `removed` requests `2.31.0` carrying a high GHSA,
    - an `added` requests `2.32.0` with no vulnerabilities,
    - an `added` `idna` `3.0` carrying a `moderate` GHSA.

    With floor `medium` it returns one hit (`idna`, `medium`), and with floor `high` it returns `[]`.
  - `test_review_changes_unknown_severity_is_a_hit`: severity `"weird"` at floor `critical` gives one hit with
    severity `"unknown"`.
  - `test_review_changes_without_the_package_is_unreadable` (Review Focus 1): `[]`, and a list naming only `idna`,
    each raise `AlertError` matching `"no change to requests"`.
  - `test_review_changes_refuses_a_hostile_reply` (parametrized, Review Focus 5): a non-list, an item that's a
    string, `vulnerabilities: "x"`, `advisory_ghsa_id: "GHSA-bad"` and an added counted item named
    `"evil\n## owned"` each raise `AlertError`.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_vuln.py --import-mode=prepend -q -k "advisory or same_package or match_package or severity or review_changes"`
Expected: FAIL with `ImportError` / `AttributeError` on the new names.

- [ ] **Step 3: Implement the interfaces above in `agentflow/vuln.py`.** `parse_advisory` reuses `_references` and
  caps `summary` and `description` at `MAX_TEXT`. `review_changes` checks the package once over all items before
  it counts hits, so an empty reply never returns `[]`.

- [ ] **Step 4: Run `test_vuln.py` whole and see it pass**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_vuln.py --import-mode=prepend -q`
Expected: PASS. Then run `uvx ruff@0.17.4 check .` and see `All checks passed!`.

- [ ] **Step 5: Commit** `Parse global advisories and dependency-graph compares in the vuln module.`

---

### Task 2: Intake: `--advisory ID --package NAME --manifest PATH`

**Files:**
- Modify: `agentflow/vuln.py`: `gather_advisory`, plus `pr_lines`, which says `Advisory: <url>` for an advisory run.
- Modify: `agentflow/intake.py` (`gather`).
- Modify: `agentflow/pipeline.py`: the input checks in `start`.
- Modify: `agentflow/flow.py`: `PARAM_FLAGS`, the `run` refusal message, and `cmd_explain` with
  `exp.add_argument("--advisory")`.
- Modify: `agentflow/conditions.py`: add `"advisory"` to `SLUG_PARAMS` after `"alert"`, only if `explain` needs it
  for a slug. `gather` sets `slug` itself.
- Modify: `agentflow/tests/bin/gh`: serve `api advisories/<GHSA>` and `api advisories?<query>` from
  `$FAKE_GH_ADVISORIES` (a JSON list file). The query filters by `cve_id`. An unknown GHSA is a 404, as for alerts.
  Update the docstring.
- Modify: `agentflow/tests/conftest.py`: the `gh_advisories` fixture.
- Test: `agentflow/tests/test_vuln.py` for intake, and `agentflow/tests/test_vuln_lane.py` for one end-to-end run.

**Interfaces:**
- Consumes: Task 1's `advisory_id`, `parse_advisory`, `match_package` and `Advisory`, and the existing `_api`
  pattern, `_dependabot_prs`, `MANIFEST_MAX`, `gitops.commit_at`, `gitops.read_blob`, `state.slugify` and
  `intake.DATA_NOTE`.
- Produces:
  - `ADVISORIES_API = "advisories"`.
  - `gather_advisory(policy: VulnPolicy, ref: object, package: object, manifest: object, repo: Path, *, fork: str)
    -> tuple[dict, dict[str, bytes], list[str]]`. It keeps `gather_alert`'s contract:
    - `params_update` holds `advisory` (the id), `slug` (`slugify(f"{package}-{first GHSA id}")`) and
      `advisory_ids`;
    - `files` holds `alert.json` and `alert.md`.

    `alert.json` holds `source: "advisory"`, `alert: null`, `alerts: []`, `ecosystem`, `package` (as the advisory
    spells it), `manifest`, `severity` (the highest of the matches), `ids` (the GHSA ids, then the CVE ids, in
    order), `advisories: [{number: null, severity, ids, vulnerable_range, patched, url}]` (one per kept
    vulnerability) and `dependabot_prs`. An alert run's `alert.json` gains `source: "dependabot"`. A
    `GhError` naming HTTP 404 for a GHSA becomes `ValueError("no GitHub advisory <id>")`.
  - `intake.gather`: when `params["advisory"]` is set, it calls `vuln.gather_advisory(vuln_policy,
    params["advisory"], params.get("package"), params.get("manifest"), repo, fork=fork_from)`.
  - `flow.PARAM_FLAGS` gains `"advisory"`, `"package"` and `"manifest"`. `module_queue.SPAWN_PARAMS` is unchanged:
    the Backlog never starts one.

- [ ] **Step 1: Write the failing intake tests** in `test_vuln.py`, beside the `--alert` intake tests
  (`test_manifest_must_exist_at_fork_commit` and the others). Add a `gh_advisories` fixture to `conftest.py`, next to
  `gh_alerts`. It is `flow_repo` with the vuln lane and `requirements.txt` committed and pushed, as `gh_alerts`
  sets it up, plus `$FAKE_GH_ADVISORIES` holding `[GLOBAL_ADVISORY]`, and it returns a handle with `write` and
  `calls`:
  - `test_gather_advisory_by_ghsa`: `facts["source"] == "advisory"`, `facts["alerts"] == []`,
    `facts["ids"] == ["GHSA-cfgh-2345-vwxj", "CVE-2024-1234"]`, `facts["advisories"][0]["patched"] == "2.32.0"`,
    `params["slug"] == "requests-ghsa-cfgh-2345-vwxj"`, and `"# Advisory GHSA-cfgh-2345-vwxj" in alert.md`. The gh
    log holds exactly `["api", "advisories/GHSA-cfgh-2345-vwxj"]`, plus the Dependabot `pr list` call.
  - `test_gather_advisory_by_cve_queries_by_cve_id`: the gh log has
    `["api", "advisories?cve_id=CVE-2024-1234&per_page=100"]`.
  - `test_gather_advisory_refuses` (parametrized, each one raising and leaving no file):
    - an unknown GHSA (`"no GitHub advisory"`);
    - `--package urllib3` (`"does not name"`);
    - the only match withdrawn;
    - `first_patched_version: null` (`"no patched version"`);
    - severity `unknown`;
    - severity `low` with `min_severity: high`;
    - a match in both `pip` and `npm` (`"more than one ecosystem"`);
    - `--manifest ../x` (`AlertError`);
    - `--manifest missing.txt` (`"not a file at project.fork_from"`);
    - `--package "a b"` (`AlertError`).
  - `test_gather_advisory_ignores_vuln_source`: with `vuln.source: ""` it still gathers.
  - `test_gather_advisory_warns_for_a_match_with_no_patch`: two vulnerabilities for requests, one with null
    `first_patched_version`, give one `advisories` entry and one warning.
  - `test_pr_lines_says_advisory_for_an_advisory_run`: `pr_lines` has
    `Advisory: https://github.com/advisories/GHSA-cfgh-2345-vwxj` and no `Alert:` line.

- [ ] **Step 2: Write the failing start and CLI tests** in `test_vuln_lane.py`:
  - `test_run_refuses_bad_advisory_input_combinations` (parametrized over `pipeline.start` params; each raises
    `ValueError` and creates no run):
    - `{"advisory": GHSA}` with no package or manifest;
    - `{"advisory": GHSA, "package": "requests"}` with no manifest;
    - `{"alert": "42", "advisory": GHSA, "package": …, "manifest": …}`;
    - `{"package": "requests"}` alone;
    - the `bug` lane with `advisory`.
  - `test_happy_path_from_an_advisory` (copy `test_happy_path_red_patch_cleared`): a vuln run started with
    `{"advisory": GHSA, "package": "requests", "manifest": "requirements.txt"}`, and no `$FAKE_GH_ALERTS`, reaches
    `advisory_cleared`, and `vuln_alert_read` is emitted with `alerts == []`.
  - `test_explain_vuln_with_an_advisory_calls_no_gh(sparse_repo)`: `flow.main(["explain", "vuln", "--advisory",
    GHSA])` exits 0 and makes no `gh` call.

- [ ] **Step 3: Run them to see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_vuln.py agentflow/tests/test_vuln_lane.py --import-mode=prepend -q -k "advisory"`
Expected: FAIL.

- [ ] **Step 4: Implement** `gather_advisory`, the fake-`gh` routes, `intake.gather`, the `pipeline.start` checks, the
  flags and `pr_lines`. Write the checks in `start` in this order, before `gather`:
  1. Both `alert` and `advisory` → "`--alert` and `--advisory` start a run each: give one".
  2. `package` or `manifest` without `advisory` → "`--package` and `--manifest` go with `--advisory`".
  3. `advisory` without both → "`--advisory` needs `--package NAME` and `--manifest PATH`".
  4. The existing lane checks, with "alert" widened to "alert or advisory": "the vuln lane starts from a Dependabot
     alert or an advisory: agentflow run vuln --alert N, or --advisory ID --package NAME --manifest PATH".

  `gather_advisory` doesn't check `policy.source`.

- [ ] **Step 5: Run both files whole and see them pass**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_vuln.py agentflow/tests/test_vuln_lane.py agentflow/tests/test_signals.py agentflow/tests/test_backlog.py --import-mode=prepend -q`
Expected: PASS. Then run `uvx ruff@0.17.4 check .` and see `All checks passed!`.

- [ ] **Step 6: Commit** `Start a vuln run from a GitHub advisory with --advisory, --package and --manifest.`

---

### Task 3: `vuln.dependency_review`: check the pushed head before the PR is marked ready

**Files:**
- Modify: `agentflow/config.py`:
  - `VulnPolicy.dependency_review: bool = False`, parsed with `_optional(vuln, "dependency_review", "vuln", bool,
    False)`;
  - `Validator.vuln`: with `true` and `vuln` not in `lanes`, an error on `vuln.dependency_review`.
- Modify: `agentflow/defaults/config.yaml`: `dependency_review: false` with a comment, under `vuln:`.
- Modify: `agentflow/config_edit.py`: add `"vuln.dependency_review"` to `_PROTECTED_KEYS`.
- Modify: `agentflow/vuln.py`: `review_dependencies`, and a `Dependency review:` line in `pr_lines`.
- Modify: `agentflow/ci_lane.py` (`run_ci`).
- Modify: `agentflow/hitl.py`: way-on texts for `new_advisory` and `dependency_review_failed`.
- Modify: `agentflow/tests/bin/gh`: `api repos/{owner}/{repo}/dependency-graph/compare/<basehead>` prints
  `$FAKE_GH_COMPARE` (a file path). `$FAKE_GH_COMPARE_STATUS=403|404` writes the matching `gh: … (HTTP 403|404)`
  and exits 1.
- Test: `agentflow/tests/test_vuln_lane.py`, `agentflow/tests/test_config_edit.py`, `agentflow/tests/test_pack.py`
  (validate).

**Interfaces:**
- Consumes: Task 1's `review_changes`, the existing `read_alert`, `gitops.commit_at`, `gitops.gh_json`,
  `GhError`, `hitl.raise_hitl`, `hitl.clean_fact`, and `CILane._pushed_sha` and `_ready`.
- Produces:
  - `REVIEW_API = "repos/{owner}/{repo}/dependency-graph/compare"`.
  - `review_dependencies(cfg: Config, run: Run, worktree: Path, key: str, head: str) -> tuple[str, str]`. It
    returns `(verdict, summary)`:
    - `"off"`: the key is false, or the run isn't vuln. Nothing is emitted.
    - `"ok"`: the check is clean.
    - `"accepted"`: a `hitl_approved` on `key` comes after a failing `dependency_reviewed` for this same `head`.
      There is no `gh` call.
    - `"new_advisory"`: the reply has hits.
    - `"dependency_review_failed"`: a `GhError` or `AlertError`, a missing origin base, or a `head` or base that
      isn't 40-hex.

    Every verdict but `off` and `accepted` emits
    `dependency_reviewed(key, head, base, ok: bool, verdict, advisories: list[dict], error: str)`.
  - `pr_lines` adds, from the last `dependency_reviewed` for the pushed head, one of:
    - `Dependency review: no new advisory at or above <floor>`;
    - `Dependency review: accepted by <by> (<ghsa ids>)`;
    - `Dependency review: not checked on the pushed head`, when the key is on and there is no event for that sha.

    There is no line when the key is off.
  - `run_ci`: on `verdict == "pass"`, it calls `review_dependencies(..., self._pushed_sha())`. On
    `new_advisory` or `dependency_review_failed` it calls `self._ci_raise(verdict, key, summary)` and returns
    `"stopped"`. Otherwise it calls `_ready`.

- [ ] **Step 1: Write the failing tests.** In `test_vuln_lane.py`, add a helper `_through_ci(flow_repo, gh_alerts,
  compare: list | None, status: str = "")`. It drives a vuln run to `ci` with `FAKE_GH_CHECKS` passing (copy the
  existing CI test setup) and `vuln: {dependency_review: true}`.
  - `test_dependency_review_clean_marks_ready`: the compare lists a `removed` requests 2.31.0 and an `added` requests
    2.32.0. `pr_marked_ready` is emitted, and the PR body has
    `Dependency review: no new advisory at or above low`.
  - `test_dependency_review_hit_stops_with_new_advisory`: an added `idna` with a high GHSA. The stop kind is
    `new_advisory`, its summary names the GHSA and `idna`, and there is no `pr_marked_ready`.
  - `test_dependency_review_empty_compare_fails_closed` (Review Focus 1): `[]` gives the stop
    `dependency_review_failed` and no `pr_marked_ready`.
  - `test_dependency_review_api_error_fails_closed` (parametrized `403`, `404`): the stop is
    `dependency_review_failed`, and its summary includes `HTTP 403` / `HTTP 404`.
  - `test_dependency_review_hostile_reply_never_reaches_the_stop` (Review Focus 5): an added package named
    `"evil\n## owned"` gives `dependency_review_failed`, and `"## owned"` is in neither the stop's summary nor
    the PR body.
  - `test_approving_new_advisory_accepts_that_sha_only` (Review Focus 4):
    1. After the `new_advisory` stop, `approve` makes `run_ci` mark the PR ready with
       `Dependency review: accepted by`, and makes no further compare call (count the gh log).
    2. A second run: approve, then append a `ci_round` event with a new pushed sha. `review_dependencies` for the
       new sha calls `gh` again.
  - `test_dependency_review_off_makes_no_call`: the key is false. There is no compare call in the gh log, and no
    `Dependency review` line.
  - `test_dependency_review_is_vuln_only`: a feature run with the key true makes no compare call.
  - In `test_pack.py`, `test_validate_dependency_review_needs_the_vuln_lane`: with `lanes: [feature, bug]` and
    `vuln: {dependency_review: true}`, the error is on `vuln.dependency_review`, and `"yes"` (a string) errors as
    not a bool.
  - In `test_config_edit.py`, `test_dependency_review_is_protected`: the Settings save of `vuln.dependency_review`
    is refused as protected, as the existing `vuln.probe` case is.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_vuln_lane.py agentflow/tests/test_pack.py agentflow/tests/test_config_edit.py --import-mode=prepend -q -k "dependency_review"`
Expected: FAIL.

- [ ] **Step 3: Implement** the config key, validate, `_PROTECTED_KEYS`, the defaults line, `review_dependencies`,
  the `run_ci` call, the `pr_lines` line, the fake-`gh` route, and the two `hitl.py` way-on texts:
  - `new_advisory`: "The pushed head adds a dependency with an advisory at or above `vuln.min_severity`. Fix it on
    the branch and `agentflow retry {run} {key}` checks again, or `agentflow approve {run} {key}` accepts this head
    (the PR names the advisories), or `agentflow cancel {run}`."
  - `dependency_review_failed`: "GitHub's dependency review could not be read for the pushed head (a 403 is a
    private repository without GitHub Advanced Security, or a fork; an empty or partial compare means the
    dependency graph has not caught up). `agentflow retry {run} {key}` asks again, `agentflow approve {run} {key}`
    accepts this head unchecked, or set `vuln.dependency_review: false`."

  Build the base with `gitops.commit_at(worktree, f"refs/remotes/origin/{cfg.project.fork_from}")`. The `gh` path
  is `f"{REVIEW_API}/{base}...{head}"`. Pass the run's `package` and `ecosystem` from `read_alert` to
  `review_changes`. The summary and each stored advisory field pass `clean_fact`.

- [ ] **Step 4: Run the touched files whole and see them pass**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_vuln_lane.py agentflow/tests/test_ci.py agentflow/tests/test_pack.py agentflow/tests/test_config_edit.py --import-mode=prepend -q`
Expected: PASS. Then run `bin/agentflow validate` and see `0 warnings`, and run `uvx ruff@0.17.4 check .` and see
`All checks passed!`.

- [ ] **Step 5: Commit** `Stop a vuln run whose pushed head adds a new advisory, behind vuln.dependency_review.`

---

### Task 4: `doctor`: the dependency review check

**Files:**
- Modify: `agentflow/doctor.py`: `_dependency_review_check(repo: Path, fork_from: str) -> Check`, called from the
  check list after `_dependabot_check` when `cfg.vuln.dependency_review` is true, `vuln` is in `lanes` and `gh` is
  on `PATH`. Update the module docstring's "only command it runs" sentence.
- Test: `agentflow/tests/test_doctor.py`

**Interfaces:**
- Consumes: `run_bounded`, `Check`, `_last_line` (existing), and Task 3's `vuln.REVIEW_API`.
- Produces: `Check("dependency review", …)`. It runs `["gh", "api", f"{REVIEW_API}/{q}...{q}"]`, where `q` is
  `urllib.parse.quote(fork_from, safe="")`:
  - exit 0 → `ok`, "dependency review readable";
  - `HTTP 403` → `warn`, fix "a private repository needs GitHub Advanced Security; a fork can't be compared; or set
    `vuln.dependency_review: false`";
  - `HTTP 404` → `warn`, fix "turn on the dependency graph in the repository's settings";
  - anything else → `warn` with the last line, as `_dependabot_check` does.

- [ ] **Step 1: Write the failing tests** next to `test_doctor_dependabot_check`. Copy its parametrized shape for
  reply and status:
  - `test_doctor_dependency_review_check` covers `ok`, `403` and `404`, and checks the argv is
    `gh api repos/{owner}/{repo}/dependency-graph/compare/main...main` (with the fixture's `fork_from`).
  - `test_doctor_dependency_review_check_runs_only_when_on`: the key is false, or `vuln` isn't in `lanes`, and no
    `dependency review` check appears.
- [ ] **Step 2: Run them to see them fail.** Run
  `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_doctor.py --import-mode=prepend -q -k dependency_review`.
  Expected: FAIL.
- [ ] **Step 3: Implement** `_dependency_review_check` and its call.
- [ ] **Step 4: Run `test_doctor.py` whole and see it pass**, then run ruff.
- [ ] **Step 5: Commit** `Check the dependency review API in doctor when vuln.dependency_review is on.`

---

### Task 5: Docs, changelog, version, full verification

**Files:**
- Modify: `docs/agentflow-playbook.md`:
  - §10: a cheat-sheet line `agentflow run vuln --advisory ID --package NAME --manifest PATH`.
  - §13: in "Patching a security alert", a paragraph on `--advisory` for repositories without Dependabot, and one on
    the dependency review check and its two stops.
  - §14: a `vuln.dependency_review` line (default, `validate` rule, Protected). Widen the `vuln.source` line: it
    doesn't apply to `--advisory`. Widen the doctor row in the table at line 488.
- Modify: `CLAUDE.md`: in the `vuln.py` architecture bullet, mention `gather_advisory` and `review_dependencies`.
  In the `doctor.py` bullet, mention the extra read-only `gh api` call.
- Modify: `docs/specs/agentflow-vuln-fix-proposal.md`: change §Rollout V3's first two bullets to "shipped in 3.8".
  Change §10's status line only if the human has approved it.
- Modify: `CHANGELOG.md`, `pyproject.toml` and `agentflow/pack.py`.

- [ ] **Step 1: Write the docs** above. Add `Added` lines under `## [Unreleased]` for `--advisory` and for
  `vuln.dependency_review`, and a `Changed` line for `doctor`'s extra check.
- [ ] **Step 2: Bump the version** to `3.8` in `pyproject.toml` and in `pack.ENGINE_VERSION`. Rename
  `[Unreleased]` to `## [3.8] - <today>` and add a fresh empty `## [Unreleased]` above it. If `main` has moved past
  3.8, bump past it.
- [ ] **Step 3: Verify.** Run each of these and see the expected output:
  - `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_packaging.py agentflow/tests/test_portability.py --import-mode=prepend -q`: PASS.
  - `bin/agentflow validate`, plus `validate` on both fixture packs: `0 warnings`.
  - `git diff --exit-code main -- agentflow/tests/fixtures/explain/`: no output.
  - The full suite, `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`:
    PASS.
  - `uvx ruff@0.17.4 check .`: `All checks passed!`.
- [ ] **Step 4: Commit** `Document the vuln lane's V3 inputs and checks, and release 3.8.`
