# Vuln lane, slice V1: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `agentflow run vuln --alert N` turns an open Dependabot alert into a conformant run and a PR that is ready
for review. The pack's audit probe proves the advisory present before the patch and gone after it.

**Architecture:** A new pure module, `agentflow/vuln.py`, holds the alert model, field validation, grouping, the
probe verdict and the engine's fixed forbid list. Intake, config, the expectation machinery (`bug_eval.py`),
conformance, the PR body, signals, starters, detect and doctor each gain a small hook that calls into it. The lane
itself is data: new steps tagged `lanes: [vuln]` in `defaults/config.yaml`.

**Tech Stack:** Python 3.12, stdlib + `pyyaml`, `gh` (argv lists only), pytest with `fake_agent.py` and
`tests/bin/gh`.

**Spec:** `docs/specs/agentflow-vuln-fix-proposal.md`. Read §Design 1–7, §Config and §Testing. V2 (Backlog sync,
§8) and V3 are out of scope.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No third-party import.
- No project strings in engine files: `test_portability.py` stays green. Scanner names and file globs are fine.
- `agentflow/tests/fixtures/explain/*.txt` stay byte-identical.
- Every new config key ships with a Settings field or a `_PROTECTED_KEYS` entry, `validate` checks, a
  `test_config_edit.py` case, and a playbook §14 line.
- Alert text is data: it never reaches a shell, a gate, or `argv` other than as a validated field. `gh` is called
  with argv lists only and `GET` only.
- Lane name `vuln`. Stop kinds `advisory_not_reproduced` and `probe_failed`. Expect values `advisory_present` and
  `advisory_cleared`. Floor obligation id `advisory_cleared`.
- `vuln.probe` defaults to `""`, and `run vuln` refuses to start while it is empty. The starters set `audit`.
- Probe "ran cleanly" means no timeout and non-empty output, and either the exit is in `ok_exit`, or the exit is in
  `found_exit` and the output names some advisory id (`ANY_ADVISORY`).
- `bin/agentflow validate` prints `0 warnings` on this repository's pack and both fixture packs.
- `uvx ruff@0.17.4 check .` prints `All checks passed!` before every commit.
- `detect.py` and `init.py` are security-overlay paths: plan and review those changes with `security-and-hardening`.

## Review Focus

1. **A scanner that dies looks clean.** A crash with exit 1 and a traceback, exit 127, empty output, or a timeout
   must be `probe_failed` and never cleared. Pinned in Task 1 (`judge`) and Task 5 (end to end).
2. **Id prefixes and substrings.** `CVE-2024-1234` must not match `CVE-2024-12345`, and a GHSA id must not match
   inside a URL slug that continues with `-`. Pinned in Task 1.
3. **Hostile alert fields.** A package named `x; rm -rf ~`, a newline in a version, a manifest `../../etc/passwd`, or
   a 1 MB description: refused at intake, or kept as data in `alert.md` only. Pinned in Task 4.
4. **Packs that don't opt in.** A sparse pack gets `vuln` in its lanes but no probe. `validate` must still print 0
   warnings, and `run vuln` must refuse with a message that names `vuln.probe`. A pack with its own `phases:` and no
   `vuln` in `lanes` is untouched. Pinned in Tasks 2 and 3.
5. **Nested ignore files.** A patch that writes `web/.nsprc` or `sub/osv-scanner.toml`, or a probe command that
   writes one, is a `guard_violation`. Pinned in Task 5.

---

### Task 1: `vuln.py`: the alert model, validation, grouping and the probe verdict

**Files:**
- Create: `agentflow/vuln.py`
- Modify: `agentflow/gates.py:85-100` (`GateResult.exit_code`), `agentflow/gates.py:337-358` (`_run_gate` sets it)
- Test: `agentflow/tests/test_vuln.py`

**Interfaces:**
- Produces:
  - `SEVERITIES = ("low", "medium", "high", "critical")`;
  - `ECOSYSTEMS = ("actions", "composer", "go", "maven", "npm", "nuget", "pip", "pub", "rubygems", "rust", "swift")`;
  - `GHSA_ID`, `CVE_ID`, `ANY_ADVISORY` (compiled `re`);
  - `ENGINE_FORBID: tuple[str, ...]`, exactly the spec's §4 list;
  - `class AlertError(ValueError)`;
  - `@dataclass(frozen=True) class Alert` with fields `number: int`, `state: str`, `ecosystem: str`,
    `package: str`, `manifest: str`, `severity: str`, `ids: tuple[str, ...]`, `vulnerable_range: str`,
    `patched: str | None`, `url: str`, `summary: str`, `description: str`, `references: tuple[str, ...]`;
  - `parse_alert(data: object) -> Alert` (raises `AlertError`);
  - `siblings(primary: Alert, others: Iterable[Alert]) -> tuple[Alert, ...]`;
  - `judge(exit_code: int | None, output: str, ids: Sequence[str], ok_exit: Sequence[int], found_exit: Sequence[int]) -> tuple[str, list[str]]`;
  - `severity_at_least(severity: str, floor: str) -> bool`;
  - `GateResult.exit_code: int | None = None`.

- [ ] **Step 1: Write the failing tests** in `agentflow/tests/test_vuln.py`.
  - `ALERT` is a dict shaped like GitHub's REST reply. It holds `number`, `state`, `html_url`,
    `dependency.package.{ecosystem,name}`, `dependency.manifest_path`, `security_advisory.{ghsa_id,cve_id,summary,description,severity,identifiers,references}`
    and `security_vulnerability.{vulnerable_version_range,first_patched_version.identifier,severity}`. Use
    `GHSA-abcd-2345-wxyz` and `CVE-2024-1234`.

```python
def test_parse_alert_keeps_validated_facts():
    a = vuln.parse_alert(ALERT)
    assert (a.number, a.ecosystem, a.package, a.manifest) == (42, "pip", "requests", "requirements.txt")
    assert a.ids == ("GHSA-abcd-2345-wxyz", "CVE-2024-1234") and a.patched == "2.32.0" and a.severity == "high"

@pytest.mark.parametrize("path,value", [
    (("dependency", "package", "name"), "x; rm -rf ~"), (("dependency", "package", "name"), "a b"),
    (("dependency", "package", "name"), "n" * 215), (("dependency", "manifest_path"), "../../etc/passwd"),
    (("dependency", "manifest_path"), "/abs/req.txt"), (("dependency", "package", "ecosystem"), "cargo"),
    (("security_vulnerability", "first_patched_version", "identifier"), "1.0\n2"),
    (("security_advisory", "ghsa_id"), "GHSA-0000-0000-0000"), (("number",), True)])
def test_parse_alert_refuses_bad_fields(path, value):
    with pytest.raises(vuln.AlertError):
        vuln.parse_alert(_set(ALERT, path, value))

def test_null_patched_version_parses_as_none():  # intake refuses it (Task 4), parse does not
    assert vuln.parse_alert(_set(ALERT, ("security_vulnerability", "first_patched_version"), None)).patched is None

def test_siblings_same_package_and_manifest_open_only():
    # same eco/package/manifest + open → kept; other manifest, other package, dismissed → dropped; primary first

@pytest.mark.parametrize("code,out,expected", [
    (1, "Found GHSA-abcd-2345-wxyz in requests", ("present", ["GHSA-abcd-2345-wxyz"])),
    (0, "No known vulnerabilities found", ("absent", [])),
    (1, "Found CVE-2024-9999 in urllib3", ("absent", [])),          # found something else: clean scan
    (1, "Traceback (most recent call last): boom", ("failed", [])),  # found-exit but no id: a crash
    (127, "pip-audit: not found", ("failed", [])), (None, "GHSA-abcd-2345-wxyz", ("failed", [])),
    (0, "", ("failed", [])), (2, "GHSA-abcd-2345-wxyz", ("failed", [])),
    (1, "see CVE-2024-12345", ("absent", [])),                        # not CVE-2024-1234
    (1, "https://x/GHSA-abcd-2345-wxyz-extra and CVE-2024-9", ("absent", []))])
def test_judge(code, out, expected):
    assert vuln.judge(code, out, ["GHSA-abcd-2345-wxyz", "CVE-2024-1234"], [0], [1]) == expected

def test_gate_result_carries_exit_code(tmp_path):  # run_gates on a `exit 3` gate → result.exit_code == 3; timeout → None
```

- [ ] **Step 2: Run them and see them fail.** Run
  `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_vuln.py --import-mode=prepend -q`.
  Expected: fails with `ModuleNotFoundError: agentflow.vuln`.

- [ ] **Step 3: Implement `agentflow/vuln.py`.**
  - The validation rules are the spec's §1 values:
    - package `^[^\s;|&$()<>`'"\\]{1,214}$`, printable;
    - manifest: relative, no `..` part, no NUL, posix;
    - versions and range `^[0-9A-Za-z.+~_\- <>=!,^*]{1,64}$`. The range has comparison operators and spaces, but no
      newline;
    - ids by `GHSA_ID` / `CVE_ID`, fullmatch. `ids` takes `ghsa_id`, then `cve_id`, then any `identifiers[].value`
      that matches, de-duplicated in order.
  - `summary`/`description` are capped at 64 KiB each, and `references` at 20 URLs. They are kept as text, never
    validated as fields.
  - `judge` returns `"failed"` unless the probe ran cleanly as the global constraint defines it. Otherwise it returns
    `"present"` with the matched ids, or `"absent"`. The whole-token match is `(?<![\w-])<id>(?![\w-])`.
  - `ANY_ADVISORY` is `(?<![\w-])(GHSA(-[23456789cfghjmpqrvwx]{4}){3}|CVE-\d{4}-\d{4,7}|GO-\d{4}-\d{4,}|PYSEC-\d{4}-\d+|RUSTSEC-\d{4}-\d{4})(?![\w-])`.
  - In `gates.py`, add `exit_code: int | None = None` to `GateResult`. `_run_gate` sets it from `code`, and leaves
    it `None` on a timeout or a skip.

- [ ] **Step 4: Run the tests again and see them pass.** Same command as Step 2. Expected: all pass. Then run
  `pytest agentflow/tests/test_gates.py` and expect it to stay green.

- [ ] **Step 5: Lint and commit.** Run `uvx ruff@0.17.4 check .`, then
  `git commit -m "Add the vuln module: alert validation, grouping and the probe verdict."`

---

### Task 2: Config: the `vuln:` block, probe exit codes, the new expectations and the floor obligation

**Files:**
- Modify: `agentflow/config.py`:
  - `VulnPolicy` after `IssuePolicy` (`:414`);
  - `Gate` (`:590`) gains `ok_exit` and `found_exit`, parsed in `_build_gates` (`:1218`);
  - `EXPECT_VALUES` (`:216`);
  - `EXPECT_EVIDENCE` (`:426`);
  - `_build_policy` (`:1553`) adds the floor;
  - the policy validation (`:2850-2900`) warns on an entry that tries to change it;
  - `Config.vuln`, built next to `issues=` (`:1649`);
  - `_Validator`: a `vuln` check.
- Modify: `agentflow/defaults/config.yaml` (the `vuln:` block only; no lane yet)
- Modify: `agentflow/config_edit.py` (fields `vuln.min_severity`, `vuln.sync`, `vuln.dependency_files`; add
  `"vuln.source", "vuln.probe", "vuln.forbid"` to `_PROTECTED_KEYS`; the gate keys are already protected by
  `gates`)
- Modify: `docs/agentflow-playbook.md` §14 (one line each for `vuln.source`, `vuln.min_severity`, `vuln.probe`,
  `vuln.sync`, `vuln.dependency_files`, `vuln.forbid`, `gates.<n>.ok_exit`, `gates.<n>.found_exit`)
- Test: `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`, `agentflow/tests/test_pack.py`

**Interfaces:**
- Consumes: `vuln.SEVERITIES` (Task 1).
- Produces:
  - `@dataclass class VulnPolicy` with `source: str = "dependabot"`, `min_severity: str = "low"`,
    `probe: str = ""`, `sync: bool = False`, `dependency_files: tuple[str, ...]` (the spec's list), and
    `forbid: tuple[str, ...] = ()`;
  - `Config.vuln: VulnPolicy`;
  - `Gate.ok_exit: tuple[int, ...] = (0,)`, `Gate.found_exit: tuple[int, ...] = (1,)`;
  - `config.ADVISORY_PRESENT = "advisory_present"`, `config.ADVISORY_CLEARED = "advisory_cleared"`;
  - `EXPECT_VALUES = ("red", "green", ADVISORY_PRESENT, ADVISORY_CLEARED)`;
  - `EXPECT_EVIDENCE = ("red_then_green", ADVISORY_CLEARED)`;
  - `config.ADVISORY_FLOOR_ID = "advisory_cleared"`.

- [ ] **Step 1: Write the failing tests.**

```python
# test_config.py
def test_vuln_defaults(cfg):
    assert cfg.vuln == VulnPolicy()  # probe "" by default
def test_advisory_floor_is_added_for_the_vuln_lane(cfg):
    ob = cfg.policy.obligations["advisory_cleared"]
    assert ob.require == (Evidence("expect", "advisory_cleared"),) and ob.lanes == ("vuln",)
def test_advisory_floor_cannot_be_nulled(tmp_cfg):  # policy.obligations.advisory_cleared: null → still present, validate warns
def test_gate_exit_codes_parse(tmp_cfg):  # {run: x, ok_exit: [0], found_exit: []} → (0,), ()

# test_pack.py (validate)
@pytest.mark.parametrize("patch,where", [
    ({"vuln": {"probe": "nope"}}, "vuln.probe"), ({"vuln": {"min_severity": "urgent"}}, "vuln.min_severity"),
    ({"vuln": {"source": "snyk"}}, "vuln.source"),
    ({"gates": {"unit": {"run": "true", "ok_exit": []}}}, "gates.unit.ok_exit"),
    ({"gates": {"unit": {"run": "true", "ok_exit": [0], "found_exit": [0]}}}, "gates.unit.found_exit"),
    ({"gates": {"unit": {"run": "true", "found_exit": [127]}}}, "gates.unit.found_exit")])
def test_validate_refuses_bad_vuln_config(tmp_cfg, patch, where): ...
def test_validate_accepts_advisory_expectations(tmp_cfg): ...  # a step with expect: advisory_present validates

# test_config_edit.py: the three form fields round-trip with source "engine" and then "pack"; the three
# protected keys are refused by the form.
```

- [ ] **Step 2: Run them and see them fail.** Run
  `pytest agentflow/tests/test_config.py agentflow/tests/test_pack.py agentflow/tests/test_config_edit.py -k "vuln or advisory or exit_codes" -q`.
  Expected: FAIL.

- [ ] **Step 3: Implement.**
  - `_build_policy` appends `ADVISORY_FLOOR_ID` right before `FLOOR_ID` and skips a layer entry with that id, as it
    does for `FLOOR_ID`.
  - The validator warns `advisory_cleared cannot be removed or changed: this entry is ignored`. It skips the floor
    in the per-lane step checks.
  - `ok_exit`/`found_exit` validation is the spec's §Config rule.
  - `vuln.dependency_files` and `vuln.forbid` must be lists of non-empty strings.

- [ ] **Step 4: Run the tests again and see them pass.** Run the Step 2 command, then `bin/agentflow validate`,
  which must print `0 warnings`.

- [ ] **Step 5: Lint and commit.** Run ruff, then
  `git commit -m "Add the vuln config block, probe exit codes, the advisory expectations and the advisory_cleared floor."`

---

### Task 3: The `vuln` lane in the engine defaults, with its prompts and the `agentflow-patch` skill

**Files:**
- Modify: `agentflow/defaults/config.yaml`:
  - `lanes: [feature, bug, vuln]`;
  - `skills.map.patch: agentflow-patch`;
  - obligations `verified` (add `vuln`) and `vuln_reviewed`;
  - `setup`, `verify`, `plan_fixes` and `fix_findings` gain `vuln` in `lanes`;
  - new steps `triage`, `advisory_red`, `patch` and `vuln_review`, exactly as in the spec's §2 table.
- Create: `agentflow/prompts/vuln_triage.md`, `agentflow/prompts/vuln_patch.md`, `agentflow/prompts/vuln_review.md`
- Create: `agentflow/skills/agentflow-patch/SKILL.md`
- Modify: `agentflow/pipeline.py:150-160` (`start` refuses `vuln` with an empty `cfg.vuln.probe`, before `gather`)
- Modify: `agentflow/config.py` (`_Validator`: the vuln lane must hold its two expectation steps)
- Modify: `agentflow/flow.py:1172` (the `lane` help names `vuln`)
- Test: `agentflow/tests/test_vuln_lane.py` (new), `agentflow/tests/test_portability.py` (unchanged, must pass)

**Interfaces:**
- Consumes: `Config.vuln`, `ADVISORY_PRESENT`/`ADVISORY_CLEARED` (Task 2).
- Produces: step ids `define/triage`, `build/advisory_red`, `build/patch` and `review/vuln_review`; the prompt
  placeholders the templates use (only existing ones: `{run_dir}`, `{slug}`, `{lane}`).
  - `tests/test_vuln_lane.py::vuln_steps() -> dict[str, list[dict]]` returns, per phase, the default steps whose
    `lanes` include `vuln`, read from `defaults/config.yaml`.
  - `tests/test_vuln_lane.py::add_vuln_lane(data: dict) -> None` is a `FlowRepo.configure(edit=...)` callback. It
    appends `vuln` to `lanes` and splices `vuln_steps()` into the python pack's phases at the same positions
    relative to `setup`/`verify`. Tasks 5–6 use it.

- [ ] **Step 1: Write the failing tests.**

```python
def test_explain_vuln_on_engine_defaults(sparse_repo, monkeypatch):
    # write vuln: {probe: unit} into the sparse pack; `agentflow explain vuln --alert 42` lists, in order:
    # define/triage, build/setup, build/advisory_red, build/patch, verify/setup, verify/verify,
    # review/vuln_review, ship/finish, ship/ci, and no branch_review, debug, regression_test or fix
def test_sparse_pack_validates_with_vuln_lane_and_no_probe(sparse_repo): ...  # 0 warnings
def test_run_vuln_refuses_without_probe(sparse_repo): ...  # ValueError naming `vuln.probe`, and no run directory
def test_validate_refuses_vuln_lane_without_its_steps(tmp_cfg): ...
    # python pack (own phases) + lanes [..., vuln] and no step tagged vuln with expect advisory_present /
    # advisory_cleared → error at `lanes` naming `advisory_red` and `patch`; with add_vuln_lane → 0 warnings
def test_prompts_point_at_the_alert_file():
    for name in ("vuln_triage.md", "vuln_patch.md", "vuln_review.md"):
        assert "{run_dir}/alert.md" in (PROMPTS / name).read_text()
```

- [ ] **Step 2: Run them and see them fail.** Run `pytest agentflow/tests/test_vuln_lane.py -q`. Expected: FAIL.

- [ ] **Step 3: Write the steps, the prompts and the skill.**
  - Each prompt follows `debug.md`'s voice: a goal line, then the input files as data, then the method.
  - `vuln_triage.md` writes `{run_dir}/triage.md` with the lines `Relationship:`, `Target version:`,
    `Bump: patch|minor|major`, `Method: bump|override|replace` and `Release date:`. It stops with `needs_human`
    when no upgrade path clears every id inside the manifest's constraints.
  - `vuln_patch.md` uses the package manager, not hand edits. It respects the repository's release-age setting,
    records `signals.release_date`, and never touches ignore lists.
  - `vuln_review.md` carries the spec's §2 checklist, including the 7-day `high` finding.
  - `SKILL.md` holds the method paragraph from §2 and the per-ecosystem commands.
  - In `config.py`'s `_Validator`, when `vuln` is in `cfg.lanes`, the lane's steps must include one step with
    `expect: advisory_present` and one with `expect: advisory_cleared`. Otherwise it is an error at `lanes`
    ("the vuln lane needs its advisory_red and patch steps; see playbook §14").

- [ ] **Step 4: Run the tests again and see them pass.** Run the Step 2 command plus `test_portability.py`,
  `test_explain_golden.py` and `test_pack.py`. Then run `bin/agentflow validate` and expect `0 warnings`.

- [ ] **Step 5: Lint and commit.** Run ruff, then
  `git commit -m "Add the vuln lane to the engine defaults with its triage, patch and review prompts and the agentflow-patch skill."`

---

### Task 4: Intake: `--alert N`

**Files:**
- Modify: `agentflow/vuln.py` (add the `gh` side)
- Modify: `agentflow/intake.py:64-88` (`gather` takes `vuln_policy` and delegates `alert`)
- Modify: `agentflow/pipeline.py:162` (pass `cfg.vuln`; print the warnings)
- Modify: `agentflow/flow.py:533` (`PARAM_FLAGS` gains `alert`), `agentflow/flow.py:741` and `:1198` (`explain`
  takes `--alert`; no `gh` call)
- Modify: `agentflow/tests/bin/gh` (the `api .../dependabot/alerts` and `pr list` replies; document them in its
  docstring)
- Test: `agentflow/tests/test_vuln.py`

**Interfaces:**
- Consumes: `parse_alert`, `siblings`, `severity_at_least` (Task 1), and `VulnPolicy` (Task 2).
- Produces:
  - `vuln.ALERT_FILE = "alert.md"`, `vuln.ALERT_JSON = "alert.json"`;
  - `vuln.gather_alert(policy: VulnPolicy, ref: object, repo: Path) -> tuple[dict, dict[str, bytes], list[str]]`
    returns `(params_update, files, warnings)`. `params_update` holds `alert` (the number as a string), `slug`
    and `advisory_ids` (a list).
  - `vuln.read_alert(run_dir: Path) -> dict` returns the parsed `alert.json` (used by Tasks 5–6);
  - `intake.gather(params, repo, epic_labels=(), vuln_policy: VulnPolicy | None = None) -> tuple[dict, dict[str, bytes], list[str]]`.
    The third item is the warnings. Update the one caller in `pipeline.py`.
  - The event `vuln_alert_read` (emitted in `intake.save` when `alert.json` is among the files) carries `alerts`
    (numbers), `ids`, `package`, `ecosystem`, `manifest`, `severity` and `dependabot_prs`.
  - The fake `gh` reads `$FAKE_GH_ALERTS`, a JSON list file. `api repos/{owner}/{repo}/dependabot/alerts/N` prints
    the one alert whose `number` is N, or exits 1 with `HTTP 404`. The list form prints the entries that match
    `state`/`package`/`ecosystem`. `$FAKE_GH_ALERTS_STATUS=403` makes both forms exit 1 with `HTTP 403`.
    `pr list --author app/dependabot` prints `$FAKE_GH_PR_LIST`, default `[]`.

- [ ] **Step 1: Write the failing tests.**

```python
def test_alert_intake_writes_data_files_and_groups_siblings(flow_repo, gh_alerts): ...
    # alert.json ids = union of 42 and sibling 43 (same manifest), 44 (other manifest) excluded;
    # alert.md starts with intake.DATA_NOTE; slug == "requests-ghsa-abcd-2345-wxyz"
@pytest.mark.parametrize("change,message", [
    ({"state": "dismissed"}, "is dismissed"), ({"first_patched_version": None}, "no patched version"),
    ({"severity": "low"}, "below vuln.min_severity")])
def test_alert_refusals_leave_no_run(flow_repo, gh_alerts, change, message): ...
def test_source_off_refuses(flow_repo, gh_alerts): ...           # vuln.source: "" → "vuln.source is off"
def test_alert_403_names_the_permission(flow_repo, gh_alerts): ... # "Dependabot alerts: read"
def test_manifest_must_exist_at_fork_commit(flow_repo, gh_alerts): ...
def test_alert_text_never_reaches_argv(flow_repo, gh_alerts): ...  # description "$(touch pwned)" → gh log argv has no text from it, no file pwned
@pytest.mark.parametrize("prs,warned", [
    ([{"number": 7, "title": "Bump requests from 2.31.0 to 2.32.0", "url": "u"}], [7]),
    ([{"number": 8, "title": "Bump requests-toolbelt to 1.0", "url": "u"}], []), ([], [])])
def test_dependabot_pr_warning(flow_repo, gh_alerts, prs, warned): ...
def test_pr_list_failure_drops_only_the_warning(flow_repo, gh_alerts): ...  # FAKE_GH_FAIL_ON="pr list"
```

  `gh_alerts` is a fixture in the test file. It writes the `ALERT`-shaped list, and sets `FAKE_GH_ALERTS` and
  `vuln.probe: unit` through `flow_repo.configure`.

- [ ] **Step 2: Run them and see them fail.** Run `pytest agentflow/tests/test_vuln.py -k "alert or dependabot" -q`.
  Expected: FAIL.

- [ ] **Step 3: Implement `gather_alert`.**
  - It calls `gh api repos/{owner}/{repo}/dependabot/alerts/<n>`, then
    `gh api repos/{owner}/{repo}/dependabot/alerts?state=open&package=<pkg>&ecosystem=<eco>&per_page=100`, both
    through `gitops.gh_json` with `cwd=repo`.
  - It checks the manifest at `project.fork_from` with `gitops.read_blob`.
  - It then calls `gh pr list --author app/dependabot --state open --search <pkg> --json number,title,url --limit 20`.
    A title matches when the package is a whole token (`(?<![\w.-])pkg(?![\w.-])`).
  - Refusals raise `ValueError` with the spec's messages. The no-patch message lists: wait, replace the package,
    or dismiss in GitHub.
  - A 403 or 404 from `gh` becomes `GhError`, with the permission hint.

- [ ] **Step 4: Run the tests again and see them pass.** Run the Step 2 command, then
  `test_bug_eval_security.py::test_issue_intake`, `test_pipeline.py` and `test_issue_status.py`, which must stay
  green.

- [ ] **Step 5: Lint and commit.** Run ruff, then
  `git commit -m "Start a vuln run from a Dependabot alert with --alert, grouping its package's open alerts and warning about an open Dependabot PR."`

---

### Task 5: The probe expectations and the lane's forbid list, end to end to verify

**Files:**
- Modify: `agentflow/bug_eval.py`:
  - `check_expect` (`:144`) handles `ADVISORY_CLEARED`;
  - `_run_gate_step` (`:214`) handles a step with `expect: advisory_present`;
  - `_forbidden_changes` (`:189`) adds `vuln.forbidden(cfg, lane)`.
- Modify: `agentflow/lanes.py:1001` and `:1150` (forbid = `sr.step.forbid` + `vuln.forbidden(cfg, lane)`)
- Modify: `agentflow/hitl.py:~619` (the way-on text for `advisory_not_reproduced` and `probe_failed`)
- Modify: `agentflow/vuln.py` (`forbidden`, `run_probe`)
- Test: `agentflow/tests/test_vuln_lane.py`

**Interfaces:**
- Consumes: `judge`, `ENGINE_FORBID` (Task 1), `Gate.ok_exit`/`found_exit` (Task 2), `add_vuln_lane` (Task 3) and
  `read_alert` (Task 4).
- Produces:
  - `vuln.forbidden(cfg: Config, lane: str) -> list[str]` returns `[*ENGINE_FORBID, *cfg.vuln.forbid]` when
    `lane == "vuln"`, else `[]`.
  - `vuln.run_probe(cfg: Config, run: Run, worktree: Path, key: str, expect: str) -> tuple[str, list[str], GateResult]`
    runs `cfg.vuln.probe` through `gates.run_gates(cfg, [probe], ...)` into `run.gate_log_dir(key)` and judges the
    log's output, without the `$ cmd` line. It emits `expectation_checked` with `key`, `expect`, `ok`, `matched`,
    `exit_code`, `log` and `tree`. `ok` is `verdict == "present"` for `advisory_present` and `verdict == "absent"`
    for `advisory_cleared`.
  - Stops:
    - `advisory_not_reproduced` when `advisory_present` judges absent;
    - `probe_failed` on `"failed"`, either way;
    - a failed `advisory_cleared` that judged present is a `GateResult("advisory_cleared", False, False, "advisory still reported: <ids>", log, tail)`
      for the fix loop.

- [ ] **Step 1: Write the failing tests.**
  - The tests use `flow_repo.configure(patch=VULN_PATCH, edit=add_vuln_lane)` and commit `deps.txt`
    (`requests==2.31.0`) and `audit.py`. `audit.py` prints `GHSA-abcd-2345-wxyz` and exits 1 while `deps.txt` is
    below 2.32.0, and prints `No known vulnerabilities found` and exits 0 otherwise.
  - The fake agent's `patch` act writes `deps.txt` with 2.32.0.

```python
def test_happy_path_red_patch_cleared(flow_repo, gh_alerts): ...
    # events: expectation_checked advisory_present ok (matched GHSA) before any agent call of build/patch,
    # then advisory_cleared ok; build/patch committed "Patch requests-ghsa-abcd-2345-wxyz"
def test_not_reproduced_stops_before_patch(flow_repo, gh_alerts): ...  # deps already 2.32.0 → stop kind, no build/patch call
@pytest.mark.parametrize("script", ["import sys; sys.exit(127)", "import sys; print('Traceback boom'); sys.exit(1)",
                                    "pass", "import time; time.sleep(999)"])
def test_probe_failed_never_green(flow_repo, gh_alerts, script): ...  # quick gate timeout for the sleep case
def test_wrong_package_exhausts_the_fix_loop(flow_repo, gh_alerts): ...  # patch edits other.txt only → fix_attempts_exhausted
@pytest.mark.parametrize("path", [".nsprc", "web/.nsprc", "sub/osv-scanner.toml", ".github/workflows/ci.yml"])
def test_ignore_file_edit_is_a_guard_violation(flow_repo, gh_alerts, path): ...
def test_probe_that_writes_an_ignore_file_is_a_guard_violation(flow_repo, gh_alerts): ...
def test_feature_lane_forbid_unchanged(flow_repo): ...  # vuln.forbidden(cfg, "feature") == []
```

- [ ] **Step 2: Run them and see them fail.** Run `pytest agentflow/tests/test_vuln_lane.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement.**
  - `_run_gate_step` sends a step whose `expect == ADVISORY_PRESENT` to `vuln.run_probe` before the `fix_loop` and
    `on_fail` branch. On `ok` it emits `step_finished` (`done`); otherwise it raises the stop.
  - `check_expect` sends `ADVISORY_CLEARED` to `vuln.run_probe`. It snapshots the tree around the probe and applies
    `_forbidden_changes`, as the red/green command does.
  - `RED`/`GREEN` behaviour is unchanged.

- [ ] **Step 4: Run the tests again and see them pass.** Run the Step 2 command, plus
  `test_bug_eval_security.py`, `test_guards.py`, `test_agent_step.py` and `test_ci.py`.

- [ ] **Step 5: Lint and commit.** Run ruff, then
  `git commit -m "Prove a vuln run's advisory present before the patch and cleared after it, and forbid scanner ignore files in the lane."`

---

### Task 6: Conformance, the PR section and the signals

**Files:**
- Modify: `agentflow/conformance.py`:
  - `_item` (`:232`) handles `expect:advisory_cleared` through a new `_advisory_cleared`;
  - `audit` (`:313`) re-runs the probe when that finding is missing and a probe is configured.
- Modify: `agentflow/feature_lane.py:1629-1677` (`render_pr_body` adds `_section("Security advisory", vuln.pr_lines(run))`
  after the issue line; `finish` emits `vuln_scope` before the push)
- Modify: `agentflow/vuln.py` (`pr_lines`, `code_change_paths`)
- Modify: `agentflow/signals.py` (`advisory_reproduced`, `vuln_code_change` and `hours_to_pr:<severity>`, with their
  direction entries)
- Test: `agentflow/tests/test_vuln_lane.py`, `agentflow/tests/test_conformance.py`, `agentflow/tests/test_signals.py`

**Interfaces:**
- Consumes: `run_probe`, `read_alert` (Tasks 4–5) and `ADVISORY_CLEARED` (Task 2).
- Produces:
  - `vuln.pr_lines(run: Run) -> list[str]`. Each line is passed through `hitl.clean_fact`:
    - `Advisories: GHSA-…, CVE-…` (validated ids only);
    - `Severity: high`;
    - ``Package: `requests` (pip) in `requirements.txt` ``;
    - `Vulnerable: < 2.32.0 · patched in 2.32.0`;
    - `Target: <Target version line of triage.md, quoted, cut to 64 chars>`;
    - one `Alert: <url>` line per alert;
    - one `Supersedes #N (Dependabot)` line per `dependabot_prs` entry;
    - `Probe: present (<matched ids>) → cleared`, from the last ok `advisory_present` and `advisory_cleared` events,
      or `Probe: not cleared` when there is none;
    - ``Code changed outside dependency files: `a.py`, `b.py` `` when the last `vuln_scope` has paths.

    It returns `[]` for a run with no `alert.json`.
  - `vuln.code_change_paths(cfg: Config, changed: Sequence[str]) -> list[str]` returns the changed paths outside
    `cfg.vuln.dependency_files`.
  - The event `vuln_scope` carries `code_paths: list[str]`.
  - Signals:
    - `advisory_reproduced` is 1.0 when an `advisory_present` check was ok;
    - `vuln_code_change` is 1.0 when the last `vuln_scope` has paths;
    - `hours_to_pr:<severity>` is the hours from `run_started` to the first `pr_opened`, keyed by
      `vuln_alert_read.severity`.

    Directions: `lower`, `lower` and `lower` with a parameter.

- [ ] **Step 1: Write the failing tests.**

```python
# test_conformance.py (pure evaluate)
def test_advisory_cleared_needs_present_then_cleared(): ...      # only cleared → missing; present then cleared → satisfied
def test_advisory_cleared_goes_stale_after_a_code_change(): ...  # step_committed after cleared (other key) → missing
# test_vuln_lane.py
def test_finish_reprobes_a_stale_clearance(flow_repo, gh_alerts): ...  # fix_findings commit after patch → audit runs probe, records on ship/finish, conformant
def test_block_mode_stops_when_still_present(flow_repo, gh_alerts): ...  # conformance_failed, nothing pushed
def test_pr_body_security_section(flow_repo, gh_alerts): ...    # lines above; alert description text absent
def test_vuln_scope_flags_code_changes(flow_repo, gh_alerts): ... # patch also edits app.py → vuln_scope.code_paths == ["app.py"]
# test_signals.py
def test_vuln_signals(): ...
```

- [ ] **Step 2: Run them and see them fail.** Run
  `pytest agentflow/tests/test_conformance.py agentflow/tests/test_vuln_lane.py agentflow/tests/test_signals.py -k "advisory or vuln or finish_reprobes or block_mode" -q`.
  Expected: FAIL.

- [ ] **Step 3: Implement.**
  - `_advisory_cleared` mirrors `_red_then_green` (`conformance.py:213`). It takes the latest non-stale ok
    `advisory_cleared` that has an earlier ok `advisory_present`. Its phase is
    `phase_where(cfg, lambda s: s.expect == ADVISORY_CLEARED)`.
  - In `audit`, after the gates rerun:
    1. If the `advisory_cleared` finding is missing, a present check exists, and `cfg.vuln.probe` is set, call
       `vuln.run_probe(..., key, ADVISORY_CLEARED)` on the snapshot tree.
    2. On `failed` or `present`, stop with `probe_failed` or `gate_failed` and the probe report.
    3. Otherwise evaluate again.

- [ ] **Step 4: Run the tests again and see them pass.** Run the Step 2 command, plus `test_feature_lane.py`,
  `test_finished.py` and `test_scorecard.py`.

- [ ] **Step 5: Lint and commit.** Run ruff, then
  `git commit -m "Audit advisory_cleared at finish, re-probing a stale clearance, and add the Security advisory PR section and vuln signals."`

---

### Task 7: Starters, detect, init and doctor

**Files:**
- Modify: `agentflow/starters/python-uv/config.yaml`, `node-pnpm/config.yaml` and `go/config.yaml` (the `audit`
  gate with the spec's §7 commands and exit codes; `vuln: {probe: audit}`), and `docs-only/config.yaml`
  (`vuln: {source: ""}`)
- Modify: `agentflow/detect.py:41` (a new `_AUDIT_WORDS`). In `_step_gate`, a step that matches it becomes a gate
  named `audit`. In `_gate_sets`, `audit` is left out of `verify` and `build`. `Detected` gains `probe: str | None`.
- Modify: `agentflow/init.py` (when `Detected.probe` is set, write `vuln: {probe: audit}` into the sparse pack)
- Modify: `agentflow/doctor.py:356` (`_dependabot_check(repo) -> Check` when `cfg.vuln.source == "dependabot"` and
  `gh` is on PATH; `_gate_checks` also checks `cfg.vuln.probe`'s programs, as a `warn` when they are missing)
- Test: `agentflow/tests/test_detect.py`, `test_init.py`, `test_doctor.py`, `test_plug_and_play.py`, `test_layers.py`

**Interfaces:**
- Consumes: `Config.vuln`, `Gate.ok_exit`/`found_exit` (Task 2).
- Produces:
  - `detect._AUDIT_WORDS = re.compile(r"\b(pip-audit|npm\s+audit|pnpm\s+audit|yarn\s+audit|osv-scanner|govulncheck|cargo\s+audit|bundle(r)?[\s-]+audit|trivy\s+fs)\b", re.I)`.
  - `doctor._dependabot_check` runs `run_bounded(["gh", "api", "repos/{owner}/{repo}/dependabot/alerts?per_page=1"], repo)`.
    - Exit 0: `ok`, "Dependabot alerts readable".
    - Output with `HTTP 403`: `fail`, with the fix "grant the token Dependabot alerts: read".
    - Output with `HTTP 404`: `warn`, "Dependabot alerts are off for this repository, or the token cannot see them".

- [ ] **Step 1: Write the failing tests.**

```python
def test_starter_python_uv_sets_the_probe(): ...  # merged layers: gates.audit.found_exit == [1], vuln.probe == "audit"
def test_starter_go_audit_exits_zero_either_way(): ...  # found_exit == []
def test_detect_proposes_audit_outside_gate_sets(tmp_git): ...  # CI step "pip-audit --strict" → gates.audit, not in verify/build, probe "audit"
def test_init_writes_vuln_probe_when_detected(tmp_git): ...
@pytest.mark.parametrize("reply,status", [("[]", "ok"), ("HTTP 403", "fail"), ("HTTP 404", "warn")])
def test_doctor_dependabot_check(repo, reply, status): ...
def test_doctor_skips_dependabot_check_when_source_off(repo): ...
def test_doctor_warns_on_missing_probe_program(repo): ...
```

  `test_plug_and_play.py` must stay green. If the python toy repo's CI gains no audit step, nothing changes for it.

- [ ] **Step 2: Run them and see them fail.** Run
  `pytest agentflow/tests/test_detect.py agentflow/tests/test_init.py agentflow/tests/test_doctor.py agentflow/tests/test_layers.py -k "audit or probe or dependabot or starter" -q`.
  Expected: FAIL.

- [ ] **Step 3: Implement.**
  - The starter files keep their header comment. Change "It sets only the gates and which gate runs when" to "It
    sets only the gates, which gate runs when, and the vuln probe".
  - `detect` treats the repository as untrusted. An audit step that also installs goes through the existing
    `_without_installs` path, unchanged.

- [ ] **Step 4: Run the tests again and see them pass.** Run the Step 2 command, plus `test_plug_and_play.py` and
  `test_portability.py`.
  - **Manual check:** in a scratch uv project with a pinned old `requests==2.31.0`, run the python-uv starter's
    audit command. It must print a GHSA or PYSEC id and exit 1.
  - Then pin a clean version and expect exit 0. Record the output lines in the commit body. If the flags differ
    from the spec, fix the spec's §7 first.

- [ ] **Step 5: Lint and commit.** Run ruff, then
  `git commit -m "Give the starters a pinned audit probe, detect audit steps in CI, and check Dependabot access in doctor."`

---

### Task 8: Docs, changelog, version, full verification

**Files:**
- Modify: `docs/agentflow-playbook.md`:
  - §10: the `run vuln --alert N` line;
  - §13: a short "Patching a security alert" paragraph covering the probe, the triage approval, what the lane never
    does, and a `found_exit` pitfall;
  - the doctor row: the Dependabot check.
- Modify: `CHANGELOG.md` (an `Added` line under `[Unreleased]`, then the release rename)
- Modify: `pyproject.toml`, `agentflow/pack.py` (`ENGINE_VERSION` 3.5 → 3.6, or past whatever `main` holds then)
- Modify: `docs/specs/agentflow-vuln-fix-proposal.md` (`Status: approved` only if the human has approved it; never
  set it yourself)

- [ ] **Step 1: Write the docs and the changelog line.** The line reads: "Added: a `vuln` lane that patches a
  Dependabot alert's package, proving the advisory present before and gone after with the pack's audit probe
  (`--alert N`, `vuln:` config, `advisory_cleared` floor obligation)."
- [ ] **Step 2: Bump the version.** Follow CLAUDE.md §Changelog steps 1–2.
- [ ] **Step 3: Verify.** Run `pytest agentflow/tests/test_packaging.py`, then `bin/agentflow validate`
  (`0 warnings`), `uvx ruff@0.17.4 check .` (`All checks passed!`), and the full suite
  `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`. Every step
  must pass.
- [ ] **Step 4: Commit.**
  `git commit -m "Document the vuln lane and release engine 3.6."`
