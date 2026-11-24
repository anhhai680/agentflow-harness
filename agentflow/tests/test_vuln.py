from __future__ import annotations

import copy
import dataclasses
import json
import urllib.parse

import pytest

from agentflow import flow, intake, pack, vuln
from agentflow.config import Gate, load_config
from agentflow.gates import run_gates
from agentflow.gitops import GhError
from agentflow.pipeline import RunOptions, start
from conftest import GLOBAL_ADVISORY, PYTHON_CONFIG, VULN_ALERT, add_vuln_lane

ALERT = VULN_ALERT


def _set(data, path, value):
    out = copy.deepcopy(data)
    node = out
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return out


def test_parse_alert_keeps_validated_facts():
    a = vuln.parse_alert(ALERT)
    assert (a.number, a.ecosystem, a.package, a.manifest) == (42, "pip", "requests", "requirements.txt")
    assert a.ids == ("GHSA-cfgh-2345-vwxj", "CVE-2024-1234") and a.patched == "2.32.0" and a.severity == "high"
    assert a.state == "open" and a.vulnerable_range == "< 2.32.0"
    assert a.references == ("https://github.com/advisories/GHSA-cfgh-2345-vwxj",)


@pytest.mark.parametrize("path,value", [
    (("dependency", "package", "name"), "x; rm -rf ~"), (("dependency", "package", "name"), "a b"),
    (("dependency", "package", "name"), "n" * 215), (("dependency", "manifest_path"), "../../etc/passwd"),
    (("dependency", "manifest_path"), "/abs/req.txt"), (("dependency", "package", "ecosystem"), "cargo"),
    (("security_vulnerability", "first_patched_version", "identifier"), "1.0\n2"),
    (("security_advisory", "ghsa_id"), "GHSA-0000-0000-0000"), (("number",), True),
    (("dependency", "package", "name"), "-rf"), (("dependency", "package", "name"), "--index-url=x"),
    (("dependency", "manifest_path"), "."), (("dependency", "manifest_path"), "./"),
    (("dependency", "manifest_path"), "")])
def test_parse_alert_refuses_bad_fields(path, value):
    with pytest.raises(vuln.AlertError):
        vuln.parse_alert(_set(ALERT, path, value))


@pytest.mark.parametrize("value", ["2.32.0 ", "2.32 0", ">2.32.0", ">=2.32.0", "2.32.0,<3", "^2.32.0", "2.*", "!2"])
def test_a_patched_version_takes_only_the_version_alphabet(value):
    """A first patched version is one version: the range characters (space, `<>=!,^*`) are refused there."""
    with pytest.raises(vuln.AlertError, match="first patched version"):
        vuln.parse_alert(_set(ALERT, ("security_vulnerability", "first_patched_version", "identifier"), value))


@pytest.mark.parametrize("value", ["2.32.0", "1.0.0-rc.1", "1.0+local_2", "1.0~beta", "v1.2.3"])
def test_a_patched_version_in_the_version_alphabet_parses(value):
    data = _set(ALERT, ("security_vulnerability", "first_patched_version", "identifier"), value)
    assert vuln.parse_alert(data).patched == value


def test_the_vulnerable_range_keeps_the_range_alphabet():
    data = _set(ALERT, ("security_vulnerability", "vulnerable_version_range"), ">= 2.0, < 2.32.0")
    assert vuln.parse_alert(data).vulnerable_range == ">= 2.0, < 2.32.0"


def test_parse_alert_refuses_a_non_object_and_missing_parts():
    for bad in (None, [], "x", {}, _set(ALERT, ("dependency",), None)):
        with pytest.raises(vuln.AlertError):
            vuln.parse_alert(bad)


def test_null_patched_version_parses_as_none():  # intake refuses it (Task 4), parse does not
    assert vuln.parse_alert(_set(ALERT, ("security_vulnerability", "first_patched_version"), None)).patched is None


def test_ids_come_from_the_advisory_and_its_identifiers_without_duplicates():
    data = _set(ALERT, ("security_advisory", "identifiers"),
                [{"value": "GHSA-cfgh-2345-vwxj"}, {"value": "CVE-2023-0001"}, {"value": "not an id"}])
    assert vuln.parse_alert(data).ids == ("GHSA-cfgh-2345-vwxj", "CVE-2024-1234", "CVE-2023-0001")


def test_text_is_kept_but_capped():
    data = _set(ALERT, ("security_advisory", "description"), "d" * (70 * 1024))
    data = _set(data, ("security_advisory", "references"), [{"url": f"https://x/{n}"} for n in range(30)])
    a = vuln.parse_alert(data)
    assert len(a.description) == 64 * 1024 and len(a.references) == 20


def test_siblings_same_package_and_manifest_open_only():
    primary = vuln.parse_alert(ALERT)
    same = vuln.parse_alert(_set(ALERT, ("number",), 43))
    other_manifest = vuln.parse_alert(_set(ALERT, ("number",), 44) | {"dependency": {
        "package": {"ecosystem": "pip", "name": "requests"}, "manifest_path": "sub/requirements.txt"}})
    other_package = vuln.parse_alert(_set(_set(ALERT, ("number",), 45), ("dependency", "package", "name"), "urllib3"))
    dismissed = vuln.parse_alert(_set(_set(ALERT, ("number",), 46), ("state",), "dismissed"))
    got = vuln.siblings(primary, [same, other_manifest, other_package, dismissed, primary])
    assert [a.number for a in got] == [42, 43]


def test_severity_at_least():
    assert vuln.severity_at_least("high", "medium") and vuln.severity_at_least("high", "high")
    assert not vuln.severity_at_least("low", "medium")
    assert vuln.severity_at_least("critical", "low")


def test_engine_forbid_is_the_specs_list():
    assert vuln.ENGINE_FORBID == (
        ".github/dependabot.yml", ".github/workflows/**", "**/.nsprc", "**/audit-ci.json*", "**/.snyk",
        "**/osv-scanner.toml", "**/.trivyignore", "**/.grype.yaml", "**/pip-audit*.toml", "**/.cargo/audit.toml")


@pytest.mark.parametrize("code,out,expected", [
    (1, "Found GHSA-cfgh-2345-vwxj in requests", ("present", ["GHSA-cfgh-2345-vwxj"])),
    (0, "No known vulnerabilities found", ("absent", [])),
    (1, "Found CVE-2024-9999 in urllib3", ("absent", [])),          # found something else: clean scan
    (1, "Traceback (most recent call last): boom", ("failed", [])),  # found-exit but no id: a crash
    (127, "pip-audit: not found", ("failed", [])), (None, "GHSA-cfgh-2345-vwxj", ("failed", [])),
    (0, "", ("failed", [])), (2, "GHSA-cfgh-2345-vwxj", ("failed", [])),
    (1, "see CVE-2024-12345", ("absent", [])),                        # not CVE-2024-1234
    (1, "https://x/GHSA-cfgh-2345-vwxj-extra and CVE-2024-9", ("failed", []))])  # no whole-token id at all
def test_judge(code, out, expected):
    assert vuln.judge(code, out, ["GHSA-cfgh-2345-vwxj", "CVE-2024-1234"], [0], [1]) == expected


def _cfg(gates):
    cfg = load_config(PYTHON_CONFIG)
    return dataclasses.replace(cfg, gates={n: Gate(name=n, run=r) for n, r in gates.items()}, gate_sets={})


def test_gate_result_carries_exit_code(tmp_git, tmp_path):
    ok, bad = run_gates(_cfg({"ok": "true", "bad": "exit 3"}), ["ok", "bad"], tmp_git, tmp_path / "logs")
    assert (ok.exit_code, bad.exit_code) == (0, 3)
    cfg = _cfg({"slow": "sleep 30"})
    cfg = dataclasses.replace(cfg, defaults=dataclasses.replace(cfg.defaults, gate_timeout_min=0.01))
    (slow,) = run_gates(cfg, ["slow"], tmp_git, tmp_path / "logs")
    assert "timed out" in slow.reason and slow.exit_code is None


# --------------------------------------------------------------------------- intake: --alert N


def _alert(number: int, **changes) -> dict:
    """`VULN_ALERT` renumbered, its ids and url following the number, with `changes` (dotted paths) applied."""
    data = _set(ALERT, ("number",), number)
    data["html_url"] = f"https://github.com/acme/app/security/dependabot/{number}"
    for path, value in changes.items():
        data = _set(data, tuple(path.split(".")), value)
    return data


SIBLING = _alert(43, **{"security_advisory.ghsa_id": "GHSA-mmpq-2345-vwxj", "security_advisory.cve_id": "CVE-2024-5678",
                       "security_advisory.identifiers": [], "security_advisory.severity": "low",
                       "security_advisory.summary": "Second advisory"})
OTHER_MANIFEST = _alert(44, **{"dependency.manifest_path": "web/requirements.txt",
                               "security_advisory.ghsa_id": "GHSA-rrvw-2345-vwxj", "security_advisory.cve_id": None,
                               "security_advisory.identifiers": []})


def _runs(flow_repo) -> list:
    runs = pack.state_dir(flow_repo.root) / "runs"
    return list(runs.iterdir()) if runs.exists() else []


def _start(flow_repo, ref="42"):
    return start(flow_repo.cfg, flow_repo.root, "vuln", {"alert": ref}, RunOptions())


def test_alert_intake_writes_data_files_and_groups_siblings(flow_repo, gh_alerts):
    gh_alerts.write([ALERT, SIBLING, OTHER_MANIFEST])
    run = _start(flow_repo)
    facts = vuln.read_alert(run.dir)
    assert facts["alerts"] == [42, 43]
    assert facts["ids"] == ["GHSA-cfgh-2345-vwxj", "CVE-2024-1234", "GHSA-mmpq-2345-vwxj", "CVE-2024-5678"]
    assert (facts["package"], facts["ecosystem"], facts["manifest"], facts["severity"]) == (
        "requests", "pip", "requirements.txt", "high")
    assert [a["patched"] for a in facts["advisories"]] == ["2.32.0", "2.32.0"]
    assert facts["dependabot_prs"] == []
    text = (run.dir / vuln.ALERT_FILE).read_text(encoding="utf-8")
    assert text.startswith(intake.DATA_NOTE)
    assert "Requests leaks credentials on redirect" in text and "Second advisory" in text
    assert "GHSA-rrvw-2345-vwxj" not in text
    params = run.state["params"]
    assert params["alert"] == "42" and params["slug"] == "requests-ghsa-cfgh-2345-vwxj"
    assert params["advisory_ids"] == facts["ids"]
    assert run.state["branch"] == "flow/vuln/requests-ghsa-cfgh-2345-vwxj"
    [event] = flow_repo.events(run, "vuln_alert_read")
    assert event["alerts"] == [42, 43] and event["ids"] == facts["ids"] and event["severity"] == "high"
    assert (event["package"], event["ecosystem"], event["manifest"], event["dependabot_prs"]) == (
        "requests", "pip", "requirements.txt", [])
    saved = {e["file"] for e in flow_repo.events(run, "intake_saved")}
    assert saved == {vuln.ALERT_FILE, vuln.ALERT_JSON}
    api = [c for c in gh_alerts.calls() if c[:1] == ["api"]]
    assert api[0] == ["api", "repos/{owner}/{repo}/dependabot/alerts/42"]
    assert api[1] == ["api", "repos/{owner}/{repo}/dependabot/alerts?state=open&package=requests&ecosystem=pip"
                             "&per_page=100"]


@pytest.mark.parametrize("change,message", [
    ({"state": "dismissed"}, "is dismissed"),
    ({"security_vulnerability.first_patched_version": None}, "no patched version"),
    ({"security_advisory.severity": "low"}, "below vuln.min_severity")])
def test_alert_refusals_leave_no_run(flow_repo, gh_alerts, change, message):
    flow_repo.configure(patch={"vuln": {"probe": "ok", "min_severity": "medium"}}, edit=add_vuln_lane)
    gh_alerts.write([_alert(42, **change)])
    with pytest.raises(ValueError, match=message) as caught:
        _start(flow_repo)
    if "patched" in message:
        assert all(word in str(caught.value) for word in ("wait", "replace the package", "dismiss"))
    assert _runs(flow_repo) == []


def test_a_bad_alert_field_is_refused_with_no_run(flow_repo, gh_alerts):
    gh_alerts.write([_alert(42, **{"dependency.package.name": "x; rm -rf ~"})])
    with pytest.raises(ValueError, match="package name"):
        _start(flow_repo)
    gh_alerts.write([_alert(42, **{"dependency.manifest_path": "../../etc/passwd"})])
    with pytest.raises(ValueError, match="manifest"):
        _start(flow_repo)
    with pytest.raises(ValueError, match="--alert"):
        _start(flow_repo, "--repo=evil/x")
    assert _runs(flow_repo) == []


def test_source_off_refuses(flow_repo, gh_alerts):
    flow_repo.configure(patch={"vuln": {"probe": "ok", "source": ""}}, edit=add_vuln_lane)
    with pytest.raises(ValueError, match="vuln.source is off"):
        _start(flow_repo)
    assert gh_alerts.calls() == [] and _runs(flow_repo) == []


def test_alert_lane_pairing(flow_repo, gh_alerts):
    with pytest.raises(ValueError, match="--alert"):
        start(flow_repo.cfg, flow_repo.root, "vuln", {"task": "bump requests"}, RunOptions())
    with pytest.raises(ValueError, match="vuln"):
        start(flow_repo.cfg, flow_repo.root, "bug", {"alert": "42"}, RunOptions())
    assert gh_alerts.calls() == [] and _runs(flow_repo) == []


@pytest.mark.parametrize("status", ["403", "404"])
def test_alert_403_names_the_permission(flow_repo, gh_alerts, monkeypatch, status):
    if status == "403":
        monkeypatch.setenv("FAKE_GH_ALERTS_STATUS", "403")
    else:
        gh_alerts.write([])
    with pytest.raises(GhError, match="Dependabot alerts: read") as caught:
        _start(flow_repo)
    assert f"HTTP {status}" in str(caught.value)
    assert _runs(flow_repo) == []


def test_manifest_must_exist_at_fork_commit(flow_repo, gh_alerts):
    gh_alerts.write([_alert(42, **{"dependency.manifest_path": "sub/requirements.txt"})])
    (flow_repo.root / "sub").mkdir()
    (flow_repo.root / "sub" / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")  # untracked
    with pytest.raises(ValueError, match=r"sub/requirements\.txt.*project\.fork_from"):
        _start(flow_repo)
    assert _runs(flow_repo) == []


def test_alert_text_never_reaches_argv(flow_repo, gh_alerts):
    hostile = "$(touch pwned) `touch pwned2`; touch pwned3"
    gh_alerts.write([_alert(42, **{"security_advisory.description": hostile + "d" * (1 << 20),
                                   "security_advisory.summary": hostile,
                                   "security_advisory.references": [{"url": "https://x/$(touch pwned4)"}]})])
    run = _start(flow_repo)
    text = (run.dir / vuln.ALERT_FILE).read_text(encoding="utf-8")
    assert hostile in text and len(text) < 2 * vuln.MAX_TEXT + 4096  # a 1 MB description is kept, capped
    assert "touch" not in (run.dir / vuln.ALERT_JSON).read_text(encoding="utf-8")  # the runner's facts hold no text
    assert not any("touch" in arg for call in gh_alerts.calls() for arg in call)
    for root in (flow_repo.root, run.dir, gh_alerts.path.parent):
        assert not list(root.glob("pwned*"))


def test_the_alerts_query_cannot_be_altered_by_the_package_name(flow_repo, gh_alerts):
    name = "req#x?state=dismissed"  # the name check lets `#`, `?` and `=` through: they must be encoded
    gh_alerts.write([_alert(42, **{"dependency.package.name": name})])
    run = _start(flow_repo)
    [listing] = [c for c in gh_alerts.calls() if c[:1] == ["api"] and "?" in c[1]]
    query = urllib.parse.urlsplit(listing[1]).query
    assert "#" not in listing[1] and query.count("&") == 3
    assert urllib.parse.parse_qs(query) == {"state": ["open"], "package": [name], "ecosystem": ["pip"],
                                            "per_page": ["100"]}
    assert vuln.read_alert(run.dir)["package"] == name


@pytest.mark.parametrize("prs,warned", [
    ([{"number": 7, "title": "Bump requests from 2.31.0 to 2.32.0", "url": "u"}], [7]),
    ([{"number": 8, "title": "Bump requests-toolbelt to 1.0", "url": "u"}], []),
    ([], [])])
def test_dependabot_pr_warning(flow_repo, gh_alerts, monkeypatch, prs, warned):
    monkeypatch.setenv("FAKE_GH_PR_LIST", json.dumps(prs))
    run = _start(flow_repo)
    assert vuln.read_alert(run.dir)["dependabot_prs"] == warned
    assert flow_repo.events(run, "vuln_alert_read")[0]["dependabot_prs"] == warned
    texts = [e["text"] for e in flow_repo.events(run, "intake_warning")]
    assert texts == [f"Dependabot PR #{n} is open for requests; this run's PR will supersede it" for n in warned]
    assert ["pr", "list", "--author", "app/dependabot", "--state", "open", "--search", "requests", "--json",
            "number,title,url", "--limit", "20"] in gh_alerts.calls()


def test_pr_list_failure_drops_only_the_warning(flow_repo, gh_alerts, monkeypatch):
    monkeypatch.setenv("FAKE_GH_PR_LIST", json.dumps([{"number": 7, "title": "Bump requests", "url": "u"}]))
    monkeypatch.setenv("FAKE_GH_FAIL_ON", "pr list")
    run = _start(flow_repo)
    assert vuln.read_alert(run.dir)["dependabot_prs"] == []
    assert flow_repo.events(run, "intake_warning") == []


def test_cli_prints_the_warning_before_started(flow_repo, gh_alerts, monkeypatch, capsys):
    from agentflow import pipeline

    monkeypatch.setenv("FAKE_GH_PR_LIST", json.dumps([{"number": 7, "title": "Bump requests to 2.32.0", "url": "u"}]))
    monkeypatch.setattr(pipeline, "advance", lambda run, cfg, opts=None: "paused")
    monkeypatch.setattr(flow, "_survive_hangup", lambda run: None)
    assert flow.main(["run", "vuln", "--alert", "42", "--config", str(flow_repo.cfg.path)]) == 0
    out = capsys.readouterr().out
    warning = "warning: Dependabot PR #7 is open for requests; this run's PR will supersede it"
    assert warning in out and out.index(warning) < out.index("Started ")


def test_cli_explain_takes_alert_and_calls_no_gh(flow_repo, gh_alerts, capsys):
    assert flow.main(["explain", "vuln", "--alert", "42", "--config", str(flow_repo.cfg.path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith('explain vuln {"alert": "42"}')
    assert "advisory_red" in out and "patch" in out
    assert gh_alerts.calls() == []


def test_read_alert_refuses_a_missing_or_empty_file(tmp_path):
    with pytest.raises(vuln.AlertError):
        vuln.read_alert(tmp_path)
    (tmp_path / vuln.ALERT_JSON).write_text(json.dumps({"ids": []}), encoding="utf-8")
    with pytest.raises(vuln.AlertError):
        vuln.read_alert(tmp_path)


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
    bad = _alert(41)
    for path, value in change.items():  # applied here, as `_alert`'s own `number` argument can't be changed
        bad = _set(bad, tuple(path.split(".")), value)
    groups, skipped = vuln.group_alerts([bad, _alert(42), "junk"], "low")
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


# --- global advisories and the dependency-graph compare (V3) ---

GOOD_VULN = {"package": {"ecosystem": "pip", "name": "requests"}, "vulnerable_version_range": "< 2.32.0",
             "first_patched_version": "2.32.0"}


def _advisory(**changes):
    return vuln.parse_advisory({**copy.deepcopy(GLOBAL_ADVISORY), **changes})


def test_parse_advisory_keeps_validated_fields():
    a = vuln.parse_advisory(GLOBAL_ADVISORY)
    assert a.ghsa == "GHSA-cfgh-2345-vwxj" and a.cve == "CVE-2024-1234" and a.severity == "high"
    assert a.withdrawn is False and a.url == "https://github.com/advisories/GHSA-cfgh-2345-vwxj"
    assert a.references == ("https://github.com/advisories/GHSA-cfgh-2345-vwxj",)
    assert a.vulnerabilities[0] == vuln.Vulnerability("pip", "requests", "< 2.32.0", "2.32.0")


def test_parse_advisory_drops_bad_vulnerabilities_not_the_advisory():
    bad = [
        {**GOOD_VULN, "package": {"ecosystem": "erlang", "name": "requests"}},
        {**GOOD_VULN, "package": {"ecosystem": "pip", "name": "x; rm -rf ~"}},
        {**GOOD_VULN, "vulnerable_version_range": "`id`"},
        {**GOOD_VULN, "first_patched_version": "1;2"},
        {**GOOD_VULN, "first_patched_version": {"identifier": "2.32.0"}},
        "not a dict",
    ]
    a = _advisory(vulnerabilities=[bad[0], copy.deepcopy(GOOD_VULN), *bad[1:]])
    assert a.vulnerabilities == (vuln.Vulnerability("pip", "requests", "< 2.32.0", "2.32.0"),)


def test_parse_advisory_allows_no_patch_and_marks_withdrawn():
    a = _advisory(withdrawn_at="2024-02-01T00:00:00Z", cve_id=None,
                  vulnerabilities=[{**GOOD_VULN, "first_patched_version": None}])
    assert a.withdrawn is True and a.cve is None and a.vulnerabilities[0].patched is None


def test_parse_advisory_blanks_a_bad_url_and_caps_text():
    a = _advisory(html_url="https://x/a b", summary="s" * (vuln.MAX_TEXT + 5))
    assert a.url == "" and len(a.summary) == vuln.MAX_TEXT


@pytest.mark.parametrize("changes", [
    {"ghsa_id": "GHSA-0000-0000-0000"}, {"cve_id": "CVE-1"}, {"severity": "severe"}, {"ghsa_id": None},
    {"vulnerabilities": "x"}])
def test_parse_advisory_refuses_bad_ids_and_severity(changes):
    with pytest.raises(vuln.AlertError):
        _advisory(**changes)


def test_parse_advisory_refuses_a_non_object():
    with pytest.raises(vuln.AlertError):
        vuln.parse_advisory([])


def test_advisory_id():
    assert vuln.advisory_id(" GHSA-cfgh-2345-vwxj ") == "GHSA-cfgh-2345-vwxj"
    assert vuln.advisory_id("CVE-2024-1234") == "CVE-2024-1234"
    for bad in ("42", "GHSA-x; ls"):
        with pytest.raises(ValueError, match="--advisory"):
            vuln.advisory_id(bad)


@pytest.mark.parametrize("ecosystem,a,b,same", [
    ("pip", "PyYAML", "pyyaml", True), ("pip", "Foo_Bar", "foo-bar", True),
    ("nuget", "Newtonsoft.Json", "newtonsoft.json", True),
    ("npm", "Lodash", "lodash", False), ("pip", "requests", "requests-oauth", False)])
def test_same_package(ecosystem, a, b, same):
    assert vuln.same_package(ecosystem, a, b) is same


def test_match_package_unions_live_matches():
    a = _advisory()
    withdrawn = _advisory(ghsa_id="GHSA-2222-3333-4444", withdrawn_at="2024-02-01T00:00:00Z")
    other = _advisory(ghsa_id="GHSA-5555-6666-7777",
                      vulnerabilities=[{**GOOD_VULN, "package": {"ecosystem": "pip", "name": "urllib3"}}])
    advisories, vulnerabilities = vuln.match_package([a, withdrawn, other], "requests")
    assert advisories == [a] and vulnerabilities == [a.vulnerabilities[0]]


def test_normalize_severity():
    assert vuln.normalize_severity("moderate") == "medium" and vuln.normalize_severity("high") == "high"
    assert vuln.normalize_severity("severe") is None and vuln.normalize_severity(None) is None


def _change(kind, name, version, *found):
    return {"change_type": kind, "name": name, "version": version, "ecosystem": "pip", "manifest": "requirements.txt",
            "vulnerabilities": [{"severity": s, "advisory_ghsa_id": g, "advisory_summary": "x",
                                 "advisory_url": "https://github.com/advisories/" + g} for g, s in found]}


COMPARE = [
    _change("removed", "requests", "2.31.0", ("GHSA-cfgh-2345-vwxj", "high")),
    _change("added", "requests", "2.32.0"),
    _change("added", "idna", "3.0", ("GHSA-2222-3333-4444", "moderate")),
]


def test_review_changes_counts_added_at_or_above_floor():
    hits = vuln.review_changes(COMPARE, "medium", "requests", "pip")
    assert hits == [{"ghsa": "GHSA-2222-3333-4444", "severity": "medium", "package": "idna", "version": "3.0",
                     "manifest": "requirements.txt"}]
    assert vuln.review_changes(COMPARE, "high", "requests", "pip") == []


def test_review_changes_unknown_severity_is_a_hit():
    reply = [_change("added", "requests", "2.32.0", ("GHSA-cfgh-2345-vwxj", "weird"))]
    hits = vuln.review_changes(reply, "critical", "requests", "pip")
    assert [h["severity"] for h in hits] == ["unknown"]


@pytest.mark.parametrize("reply", [[], [_change("added", "idna", "3.0")]])
def test_review_changes_without_the_package_is_unreadable(reply):
    with pytest.raises(vuln.AlertError, match="no change to requests"):
        vuln.review_changes(reply, "medium", "requests", "pip")


def _with(index, **changes):
    reply = copy.deepcopy(COMPARE)
    reply[index].update(changes)
    return reply


@pytest.mark.parametrize("reply", [
    {"a": 1}, [*COMPARE, "oops"], _with(1, vulnerabilities="x"),
    _with(0, vulnerabilities=[{"severity": "high", "advisory_ghsa_id": "GHSA-bad"}]),
    _with(2, name="evil\n## owned"), _with(2, version="1;2"), _with(2, manifest="../x")])
def test_review_changes_refuses_a_hostile_reply(reply):
    with pytest.raises(vuln.AlertError):
        vuln.review_changes(reply, "medium", "requests", "pip")
