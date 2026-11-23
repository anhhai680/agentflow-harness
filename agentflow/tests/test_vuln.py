from __future__ import annotations

import copy
import dataclasses

import pytest

from agentflow import vuln
from agentflow.config import Gate, load_config
from agentflow.gates import run_gates
from conftest import PYTHON_CONFIG, VULN_ALERT

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
    (("security_advisory", "ghsa_id"), "GHSA-0000-0000-0000"), (("number",), True)])
def test_parse_alert_refuses_bad_fields(path, value):
    with pytest.raises(vuln.AlertError):
        vuln.parse_alert(_set(ALERT, path, value))


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
