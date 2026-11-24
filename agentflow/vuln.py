"""The vuln lane's alert model, its validation, its intake from GitHub and the probe verdict.

Everything but `gather_alert`, `gather_advisory`, `read_alert` and `review_dependencies` is a pure function.
`gather_alert` reads a Dependabot alert, and `gather_advisory` a global advisory, through `gh` (an argv list, never a
shell) before any run exists, `read_alert` reads back the facts they kept (`alert.json`), and `review_dependencies`
asks GitHub's dependency graph what the pushed head adds (`vuln.dependency_review`).

Trust model. A Dependabot alert is data from outside the run: GitHub's reply carries text that other people wrote
(package names, advisory summaries, version ranges). `parse_alert` is the one place that turns it into `Alert`, and it
refuses the whole alert when a field the runner *uses* (the ecosystem, package, manifest path, versions, advisory ids)
doesn't match its pattern. A refusal is an `AlertError`, never a repair. The free text (`summary`, `description`,
`references`) is kept only to show to the agent, capped in size, and is never used as a command, a path or a gate.

`judge` is the verdict of the probe, the pack's own scanner command, on whether the alert's advisory is still there. A
scanner that dies must never read as a clean scan, so `judge` answers "failed" unless the probe ran cleanly.

`config.py` imports this module's constants at module level, so this module imports nothing from `agentflow` at
module level (`config`, `gates` and `state` would be a cycle): `gather_alert` and `gather_advisory` import `gitops`,
`intake` and `state` when they run, `run_probe` imports `config`, `gates` and `guards`, and `review_dependencies`
imports `gitops`, `hitl` and `autonomy`.
"""

from __future__ import annotations

import json
import posixpath
import re
import urllib.parse
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentflow.config import Config, VulnPolicy
    from agentflow.gates import GateResult
    from agentflow.state import Run

SEVERITIES = ("low", "medium", "high", "critical")
# The ecosystems GitHub documents for Dependabot alerts.
ECOSYSTEMS = ("actions", "composer", "go", "maven", "npm", "nuget", "pip", "pub", "rubygems", "rust", "swift")

GHSA_ID = re.compile(r"^GHSA(-[23456789cfghjmpqrvwx]{4}){3}$")
CVE_ID = re.compile(r"^CVE-\d{4}-\d{4,7}$")
# An advisory id of any kind, as a whole token: what a scanner's output names when it found something.
ANY_ADVISORY = re.compile(
    r"(?<![\w-])(GHSA(-[23456789cfghjmpqrvwx]{4}){3}|CVE-\d{4}-\d{4,7}|GO-\d{4}-\d{4,}|PYSEC-\d{4}-\d+"
    r"|RUSTSEC-\d{4}-\d{4})(?![\w-])")

# Files that silence a scanner: an edit to one in a vuln run is a moved baseline, not a fix. A pack adds to the list
# (`vuln.forbid`) and can never drop an entry.
ENGINE_FORBID: tuple[str, ...] = (
    ".github/dependabot.yml", ".github/workflows/**", "**/.nsprc", "**/audit-ci.json*", "**/.snyk",
    "**/osv-scanner.toml", "**/.trivyignore", "**/.grype.yaml", "**/pip-audit*.toml", "**/.cargo/audit.toml")

_PACKAGE = re.compile(r"^[^\s;|&$()<>`'\"\\]{1,214}$")
_VERSION = re.compile(r"^[0-9A-Za-z.+~_-]{1,64}$")  # one version: a first patched version
_RANGE = re.compile(r"^[0-9A-Za-z.+~_\- <>=!,^*]{1,64}$")  # a vulnerable range: versions, comparators and commas
MAX_TEXT = 64 * 1024  # characters kept of an advisory's summary and of its description
MAX_REFERENCES = 20


class AlertError(ValueError):
    """An alert field failed its check: the message names the field, and the caller shows it and stops."""


@dataclass(frozen=True)
class Alert:
    number: int
    state: str
    ecosystem: str
    package: str
    manifest: str
    severity: str
    ids: tuple[str, ...]
    vulnerable_range: str
    patched: str | None
    url: str
    summary: str
    description: str
    references: tuple[str, ...]


def _obj(data: object, what: str) -> dict:
    if not isinstance(data, dict):
        raise AlertError(f"{what} is missing or not an object")
    return data


def _text(data: dict, key: str, what: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise AlertError(f"{what} is missing or not text")
    return value


def _package(name: str) -> str:
    if not _PACKAGE.fullmatch(name) or not name.isprintable() or name.startswith("-"):
        raise AlertError("the package name is empty, too long, starts with '-', or has whitespace or shell characters")
    return name


def _manifest(path: str) -> str:
    pure = PurePosixPath(path)
    if (not path or "\x00" in path or "\\" in path or pure.is_absolute() or ".." in pure.parts
            or not path.isprintable() or not pure.parts or posixpath.normpath(path) == "."):
        raise AlertError("the manifest path must name a file: relative, with no '..', and printable")
    return path


def _version(value: str, what: str, pattern: re.Pattern[str] = _VERSION) -> str:
    if not pattern.fullmatch(value):
        raise AlertError(f"{what} is not a version" if pattern is _VERSION else f"{what} is not a version range")
    return value


def _ids(advisory: dict) -> tuple[str, ...]:
    ghsa, cve = advisory.get("ghsa_id"), advisory.get("cve_id")
    if not isinstance(ghsa, str) or not GHSA_ID.fullmatch(ghsa):
        raise AlertError("the advisory's ghsa_id is not a GHSA id")
    found = [ghsa]
    if cve is not None:
        if not isinstance(cve, str) or not CVE_ID.fullmatch(cve):
            raise AlertError("the advisory's cve_id is not a CVE id")
        found.append(cve)
    for item in advisory.get("identifiers") or []:
        value = item.get("value") if isinstance(item, dict) else None
        if isinstance(value, str) and (GHSA_ID.fullmatch(value) or CVE_ID.fullmatch(value)) and value not in found:
            found.append(value)
    return tuple(found)


def _references(advisory: dict) -> tuple[str, ...]:
    urls = [r.get("url") for r in advisory.get("references") or [] if isinstance(r, dict)]
    return tuple(u for u in urls if isinstance(u, str))[:MAX_REFERENCES]


def parse_alert(data: object) -> Alert:
    """The validated facts of one Dependabot alert (GitHub's REST shape); `AlertError` when a used field is bad."""
    root = _obj(data, "the alert")
    number = root.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise AlertError("the alert number is not a positive integer")
    dependency = _obj(root.get("dependency"), "the alert's dependency")
    package = _obj(dependency.get("package"), "the alert's package")
    advisory = _obj(root.get("security_advisory"), "the alert's advisory")
    vulnerability = _obj(root.get("security_vulnerability"), "the alert's vulnerability")
    ecosystem = _text(package, "ecosystem", "the ecosystem")
    if ecosystem not in ECOSYSTEMS:
        raise AlertError(f"the ecosystem {ecosystem!r} is not one GitHub documents")
    severity = _text(advisory, "severity", "the severity")
    if severity not in SEVERITIES:
        raise AlertError(f"the severity {severity!r} is not one of {', '.join(SEVERITIES)}")
    fixed = vulnerability.get("first_patched_version")
    patched = None
    if fixed is not None:
        patched = _version(_text(_obj(fixed, "first_patched_version"), "identifier", "the first patched version"),
                           "the first patched version")
    return Alert(
        number=number, state=_text(root, "state", "the state"), ecosystem=ecosystem,
        package=_package(_text(package, "name", "the package name")),
        manifest=_manifest(_text(dependency, "manifest_path", "the manifest path")),
        severity=severity, ids=_ids(advisory),
        vulnerable_range=_version(_text(vulnerability, "vulnerable_version_range", "the vulnerable range"),
                                  "the vulnerable range", _RANGE),
        patched=patched, url=str(root.get("html_url") or ""),
        summary=str(advisory.get("summary") or "")[:MAX_TEXT],
        description=str(advisory.get("description") or "")[:MAX_TEXT],
        references=_references(advisory))


@dataclass(frozen=True)
class Vulnerability:
    """One package a global advisory names: the ecosystem, the package, the vulnerable range and the first patch."""
    ecosystem: str
    package: str
    vulnerable_range: str
    patched: str | None


@dataclass(frozen=True)
class Advisory:
    ghsa: str
    cve: str | None
    severity: str  # one of SEVERITIES, or "unknown"
    withdrawn: bool
    url: str  # "" unless it is a plain https URL
    summary: str
    description: str
    references: tuple[str, ...]
    vulnerabilities: tuple[Vulnerability, ...]


def advisory_id(ref: object) -> str:
    """The GHSA or CVE id in `ref`, stripped; anything else is refused (it would reach `gh` as part of a path)."""
    text = str(ref).strip()
    if not (GHSA_ID.fullmatch(text) or CVE_ID.fullmatch(text)):
        raise ValueError(f"--advisory takes a GHSA or CVE id such as GHSA-cfgh-2345-vwxj, not {str(ref)[:80]!r}")
    return text


def _vulnerability(entry: object) -> Vulnerability | None:
    """One `vulnerabilities[]` entry of a global advisory, or None when any used field fails its check: GitHub lists
    packages this lane has no use for, and one bad entry must not hide the good ones."""
    try:
        root = _obj(entry, "the vulnerability")
        package = _obj(root.get("package"), "the vulnerability's package")
        ecosystem = _text(package, "ecosystem", "the ecosystem")
        if ecosystem not in ECOSYSTEMS:
            return None
        fixed = root.get("first_patched_version")
        patched = None if fixed is None else _version(_text(root, "first_patched_version", "the first patched version"),
                                                      "the first patched version")
        return Vulnerability(
            ecosystem, _package(_text(package, "name", "the package name")),
            _version(_text(root, "vulnerable_version_range", "the vulnerable range"), "the vulnerable range", _RANGE),
            patched)
    except AlertError:
        return None


def _advisory_references(advisory: dict) -> tuple[str, ...]:
    """A global advisory's `references` are plain URL strings (a Dependabot alert's are `{url}` objects)."""
    refs = advisory.get("references")
    return tuple(r for r in refs if isinstance(r, str))[:MAX_REFERENCES] if isinstance(refs, list) else ()


def parse_advisory(data: object) -> Advisory:
    """The validated facts of one global advisory (GitHub's REST shape); `AlertError` when an id or the severity is
    bad. An entry of `vulnerabilities` that fails its checks is dropped, never repaired."""
    root = _obj(data, "the advisory")
    ids = _ids(root)
    severity = _text(root, "severity", "the severity")
    if severity not in (*SEVERITIES, "unknown"):
        raise AlertError(f"the severity {severity!r} is not one of {', '.join((*SEVERITIES, 'unknown'))}")
    entries = root.get("vulnerabilities")
    if entries is None:  # GitHub documents it as "array of objects or null": an advisory that names no package
        entries = []
    if not isinstance(entries, list):
        raise AlertError("the advisory's vulnerabilities are not a list")
    found = (_vulnerability(entry) for entry in entries)
    return Advisory(
        ghsa=ids[0], cve=ids[1] if root.get("cve_id") is not None else None, severity=severity,
        withdrawn=root.get("withdrawn_at") is not None, url=_url(str(root.get("html_url") or "")),
        summary=str(root.get("summary") or "")[:MAX_TEXT], description=str(root.get("description") or "")[:MAX_TEXT],
        references=_advisory_references(root), vulnerabilities=tuple(v for v in found if v is not None))


def same_package(ecosystem: str, a: str, b: str) -> bool:
    """True when `a` and `b` name one package in `ecosystem`: pip folds `-`, `_` and `.` and the case (PEP 503), NuGet
    and Composer fold the case, and every other registry compares the names exactly."""
    if ecosystem == "pip":
        return re.sub(r"[-_.]+", "-", a).lower() == re.sub(r"[-_.]+", "-", b).lower()
    if ecosystem in ("nuget", "composer"):
        return a.casefold() == b.casefold()
    return a == b


def match_package(advisories: Sequence[Advisory], package: str) -> tuple[list[Advisory], list[Vulnerability]]:
    """The advisories that aren't withdrawn and name `package`, and those vulnerabilities, in input order."""
    live: list[Advisory] = []
    named: list[Vulnerability] = []
    for advisory in advisories:
        if advisory.withdrawn:
            continue
        hits = [v for v in advisory.vulnerabilities if same_package(v.ecosystem, v.package, package)]
        if hits:
            live.append(advisory)
            named.extend(hits)
    return live, named


def siblings(primary: Alert, others: Iterable[Alert]) -> tuple[Alert, ...]:
    """`primary` first, then the other open alerts for the same package in the same manifest."""
    key = (primary.ecosystem, primary.package, primary.manifest)
    same = (a for a in others
            if a.number != primary.number and a.state == "open" and (a.ecosystem, a.package, a.manifest) == key)
    return (primary, *same)


def severity_at_least(severity: str, floor: str) -> bool:
    """True when `severity` ranks at or above `floor`; an unknown name never does."""
    return severity in SEVERITIES and floor in SEVERITIES and SEVERITIES.index(severity) >= SEVERITIES.index(floor)


def judge(exit_code: int | None, output: str, ids: Sequence[str], ok_exit: Sequence[int],
          found_exit: Sequence[int]) -> tuple[str, list[str]]:
    """The probe's verdict: ("failed", []), ("present", matched ids) or ("absent", []).

    The probe ran cleanly when it didn't time out (`exit_code` is not None), printed something, and either exited in
    `ok_exit`, or exited in `found_exit` with an advisory id of any kind in its output. A scanner that crashes with the
    same exit code as "found something" names no id, so it is "failed" and never a clean scan.
    """
    if exit_code is None or not output.strip():
        return "failed", []
    if exit_code not in ok_exit and not (exit_code in found_exit and ANY_ADVISORY.search(output)):
        return "failed", []
    matched = [i for i in ids if re.search(rf"(?<![\w-]){re.escape(i)}(?![\w-])", output)]
    return ("present", matched) if matched else ("absent", [])


def normalize_severity(value: object) -> str | None:
    """`medium` for the dependency graph's `moderate`, a member of `SEVERITIES` as it is, and None for anything else."""
    if value == "moderate":
        return "medium"
    return value if isinstance(value, str) and value in SEVERITIES else None


def review_changes(changes: object, floor: str, package: str, ecosystem: str) -> list[dict]:
    """The new vulnerabilities a dependency-graph compare reports, at or above `floor`: one dict per vulnerability of
    an `added` entry, `{"ghsa", "severity", "package", "version", "manifest"}`. A severity that isn't one this module
    knows counts as a hit and shows as "unknown". A reply that isn't the documented shape, or that names no change to
    `package` (the graph hasn't caught up with the push yet), is an `AlertError`, never a clean compare."""
    if not isinstance(changes, list):
        raise AlertError("the dependency graph's reply is not a list")
    seen = False
    for item in changes:
        root = _obj(item, "a dependency change")
        found = root.get("vulnerabilities")
        if not isinstance(found, list):
            raise AlertError("a dependency change's vulnerabilities are missing or not a list")
        for entry in found:
            ghsa = _obj(entry, "a dependency change's vulnerability").get("advisory_ghsa_id")
            if not isinstance(ghsa, str) or not GHSA_ID.fullmatch(ghsa):
                raise AlertError("a dependency change's advisory_ghsa_id is not a GHSA id")
        name = root.get("name")
        seen = seen or (isinstance(name, str) and same_package(ecosystem, name, package))
    if not seen:
        raise AlertError(f"the dependency graph shows no change to {package} yet")
    hits: list[dict] = []
    for item in changes:
        if item.get("change_type") != "added":
            continue
        counted = []
        for entry in item["vulnerabilities"]:
            severity = normalize_severity(entry.get("severity"))
            if severity is None or severity_at_least(severity, floor):
                counted.append((entry["advisory_ghsa_id"], severity or "unknown"))
        if not counted:
            continue
        name = _package(_text(item, "name", "a dependency change's name"))
        version = _version(_text(item, "version", "a dependency change's version"), "a dependency change's version")
        manifest = _manifest(_text(item, "manifest", "a dependency change's manifest"))
        hits.extend({"ghsa": ghsa, "severity": severity, "package": name, "version": version, "manifest": manifest}
                    for ghsa, severity in counted)
    return hits


def forbidden(cfg: Config, lane: str) -> list[str]:
    """The paths a run of `lane` may never change, on top of `guardrails.forbidden_paths` and a step's `forbid`: in a
    vuln run, `ENGINE_FORBID` and then the pack's `vuln.forbid` (it adds, never replaces); in any other lane none."""
    return [*ENGINE_FORBID, *cfg.vuln.forbid] if lane == "vuln" else []


def run_forbidden(cfg: Config, run: Run) -> list[str]:
    """`forbidden` for the lane `run` walks."""
    return forbidden(cfg, str(run.state.get("pipeline") or ""))


def failure_reason(exit_code: int | None, output: str, ok_exit: Sequence[int], found_exit: Sequence[int]) -> str:
    """Why `judge` said "failed" for these inputs ("" when it would not): what the stop and the event tell a human."""
    if exit_code is None:
        return "it timed out or did not start"
    if not output.strip():
        return f"it printed nothing (exit {exit_code})"
    if exit_code in found_exit and not ANY_ADVISORY.search(output):
        return f"exit {exit_code} says it found something, but its output names no advisory id"
    if exit_code not in ok_exit and exit_code not in found_exit:
        return f"exit {exit_code} is in neither ok_exit {list(ok_exit)} nor found_exit {list(found_exit)}"
    return ""


def _probe_output(result: GateResult, command: str) -> str:
    """The probe's own output, from its gate log without the runner's `$ <command>` line ("" when there is no log, or
    the log does not start with that line: no output is never a clean scan)."""
    if result.log_path is None:
        return ""
    try:
        text = Path(result.log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    head = f"$ {command}\n"
    return text[len(head):] if text.startswith(head) else ""


PROBE_LOG_DIR = "probe"  # under the step's gate log dir, so the probe's log never overwrites a gate's of the same name


def run_probe(cfg: Config, run: Run, worktree: Path, key: str, expect: str, *,
              guard: Callable[[str], list[str]] | None = None) -> tuple[str, list[str], GateResult]:
    """Run the pack's probe gate (`vuln.probe`) in `worktree` for the step `key` and judge its output against the run's
    advisory ids (`alert.json`): `(verdict, matched ids, the gate's result)`. Its log is `<gate log dir>/probe/`.

    Emits `expectation_checked` with `ok` (`present` for `advisory_present`, `absent` for `advisory_cleared`), the
    `verdict`, the `tree` the probe ran on, and for a "failed" verdict the `reason` (also the result's `reason`).
    `guard`, called with that tree once the probe has run and before the event is written, returns the forbidden
    paths it finds (left in the tree, or written by the probe): any makes `ok` false and goes in `forbidden`, so no
    evidence is ever recorded for a tree the guards refuse. The caller acts on them; the verdict stays the probe's.

    The verdict is "failed", and nothing is run, when the run's ids cannot be read or `vuln.probe` names no gate: a
    probe that cannot run is never a clean scan."""
    from agentflow.config import ADVISORY_CLEARED, ADVISORY_PRESENT
    from agentflow.gates import GateResult, run_gates
    from agentflow.guards import snapshot

    if expect not in (ADVISORY_PRESENT, ADVISORY_CLEARED):
        raise ValueError(f"not a probe expectation: {expect!r}")
    name = cfg.vuln.probe
    tree = snapshot(worktree)
    why = ""
    try:
        ids = [str(i) for i in read_alert(run.dir)["ids"]]
    except AlertError as exc:
        ids, why = [], str(exc)
    if not why and name not in cfg.gates:
        why = f"vuln.probe {name!r} is not a gate of the pack"
    if why:
        result = GateResult(name or "probe", False, False, why, None, f"The probe did not run: {why}.")
        run.emit("expectation_checked", key=key, expect=expect, gate=name, ok=False, verdict="failed", matched=[],
                 exit_code=None, log=None, tree=tree, reason=why, forbidden=[])
        return "failed", [], result
    gate = cfg.gates[name]
    [result] = run_gates(cfg, [name], worktree, run.gate_log_dir(key) / PROBE_LOG_DIR)
    output = _probe_output(result, gate.run)
    verdict, matched = judge(result.exit_code, output, ids, gate.ok_exit, gate.found_exit)
    reason = ""
    if verdict == "failed":
        reason = failure_reason(result.exit_code, output, gate.ok_exit, gate.found_exit)
        if result.reason and result.exit_code is None:  # the gate runner's own word: "timed out after …", a skip
            reason = f"{reason}: {result.reason}"
        result = replace(result, reason=reason)
    found = guard(tree) if guard is not None else []
    ok = verdict == ("present" if expect == ADVISORY_PRESENT else "absent") and not found
    run.emit("expectation_checked", key=key, expect=expect, gate=name, ok=ok, verdict=verdict, matched=matched,
             exit_code=result.exit_code, log=str(result.log_path) if result.log_path else None, tree=tree,
             reason=reason, forbidden=found)
    return verdict, matched, result


# --------------------------------------------------------------------------- intake: `--alert N`

ALERT_FILE = "alert.md"  # the advisory's text, for the agent (mirrored into its workspace)
ALERT_JSON = "alert.json"  # the validated facts, for the runner only
ALERTS_API = "repos/{owner}/{repo}/dependabot/alerts"  # `gh api` fills in `{owner}` and `{repo}` itself
MANIFEST_MAX = 64 << 20  # bytes of the manifest `gather_alert` reads to prove it is a file at the fork commit
PR_SEARCH_LIMIT = 20
PERMISSION_HINT = ("The alert must exist, and gh's token must be allowed to read it: a fine-grained token needs the "
                   "repository permission \"Dependabot alerts: read\", a classic token the `security_events` scope "
                   "(`gh auth refresh -s security_events`).")
_URL = re.compile(r"^https://[A-Za-z0-9.-]{1,253}(:\d{1,5})?/[A-Za-z0-9._~/%-]{0,400}$")
_CLOSED_STATES = ("dismissed", "fixed", "auto_dismissed")


def alert_number(ref: object) -> str:
    """`42` from `42` or `#42`; anything else is refused (it would reach `gh` as part of an argument)."""
    from agentflow.intake import ISSUE_REF

    match = ISSUE_REF.match(str(ref).strip())
    if not match:
        raise ValueError(f"--alert takes a Dependabot alert number such as 42, not {str(ref)[:80]!r}")
    return match.group(1)


def _api(path: str, repo: Path, number: str) -> object:
    """`gh api <path>`'s JSON; a 403 or a 404 becomes a `GhError` that says which permission the token needs."""
    from agentflow.gitops import GhError, gh_json

    try:
        return gh_json(["api", path], cwd=repo)
    except GhError as exc:
        text = str(exc)
        if "HTTP 403" in text or "HTTP 404" in text:
            raise GhError(f"gh could not read Dependabot alert #{number} ({text[:300]}). {PERMISSION_HINT}") from exc
        raise


ALERTS_MAX = 100  # alerts one listing call asks for (`per_page`)
_GROUP_KEYS = ("ecosystem", "package", "manifest", "severity", "alerts", "fixable", "ids", "url", "dependabot_prs")


def severity_query(floor: str) -> str:
    """The severities at or above `floor`, comma-joined for the alerts API's `severity` filter."""
    return ",".join(s for s in SEVERITIES if severity_at_least(s, floor))


def group_alerts(listing: object, floor: str) -> tuple[list[dict], int]:
    """The open alerts at or above `floor`, grouped by package and manifest in first-seen order, and how many
    entries were skipped as unreadable. One bad entry never stops the rest; a listing that is no list does."""
    if not isinstance(listing, list):
        raise AlertError("the alerts listing is not a list")
    skipped = 0
    found: dict[tuple[str, str, str], dict[int, Alert]] = {}
    for entry in listing:
        try:
            alert = parse_alert(entry)
        except AlertError:
            skipped += 1
            continue
        if alert.state == "open" and severity_at_least(alert.severity, floor):
            found.setdefault((alert.ecosystem, alert.package, alert.manifest), {})[alert.number] = alert
    groups = []
    for (ecosystem, package, manifest), by_number in found.items():
        members = [by_number[n] for n in sorted(by_number)]
        fixable = [a for a in members if a.patched is not None]
        ids = list(dict.fromkeys(i for a in members for i in a.ids))
        groups.append({
            "ecosystem": ecosystem, "package": package, "manifest": manifest,
            "severity": max((a.severity for a in members), key=SEVERITIES.index),
            "alerts": [a.number for a in members], "fixable": [a.number for a in fixable], "ids": ids,
            "url": _url((fixable or members)[0].url), "dependabot_prs": []})
    return groups, skipped


def _numbers(value: object) -> list[int] | None:
    """`value` when it is a list of positive integers (no bools); None otherwise."""
    if isinstance(value, list) and all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in value):
        return value
    return None


def check_group(value: object) -> dict | None:
    """A cached group, checked again as untrusted input with the same validators `parse_alert` uses: a copy of
    it when every field passes, None when any does not."""
    if not isinstance(value, dict) or set(value) != set(_GROUP_KEYS):
        return None
    alerts, fixable, prs = _numbers(value["alerts"]), _numbers(value["fixable"]), _numbers(value["dependabot_prs"])
    ids, url = value["ids"], value["url"]
    if (not alerts or alerts != sorted(set(alerts)) or fixable is None or fixable != sorted(set(fixable))
            or not set(fixable) <= set(alerts) or prs is None):
        return None
    if not isinstance(ids, list) or not all(isinstance(i, str) and (GHSA_ID.fullmatch(i) or CVE_ID.fullmatch(i))
                                            for i in ids):
        return None
    if not isinstance(url, str) or (url and _url(url) != url):
        return None
    if value["ecosystem"] not in ECOSYSTEMS or value["severity"] not in SEVERITIES:
        return None
    if not isinstance(value["package"], str) or not isinstance(value["manifest"], str):
        return None
    try:
        _package(value["package"])
        _manifest(value["manifest"])
    except AlertError:
        return None
    return {**value, "alerts": list(alerts), "fixable": list(fixable), "ids": list(ids), "dependabot_prs": list(prs)}


def _label(entry: object) -> str:
    number = entry.get("number") if isinstance(entry, dict) else None
    return f"#{number}" if isinstance(number, int) and not isinstance(number, bool) else "with no number"


def _url(text: str) -> str:
    """`text` when it is a plain https URL (no space, quote, bracket or markup), else ""."""
    return text if _URL.fullmatch(text) else ""


def prs_naming(package: str, prs: object) -> list[int]:
    """The numbers of the PRs in a parsed `gh pr list` reply whose title names `package` as a whole token."""
    token = re.compile(rf"(?<![\w.-]){re.escape(package)}(?![\w.-])", re.IGNORECASE)
    found: list[int] = []
    for pr in prs if isinstance(prs, list) else []:
        number, title = (pr.get("number"), pr.get("title")) if isinstance(pr, dict) else (None, None)
        if (isinstance(number, int) and not isinstance(number, bool) and number > 0 and isinstance(title, str)
                and token.search(title) and number not in found):
            found.append(number)
    return found


def _dependabot_prs(package: str, repo: Path) -> list[int]:
    """The open Dependabot PRs whose title names `package` as a whole token; [] when `gh` fails or prints junk."""
    from agentflow.gitops import GhError, gh_json

    try:
        data = gh_json(["pr", "list", "--author", "app/dependabot", "--state", "open", "--search", package,
                        "--json", "number,title,url", "--limit", str(PR_SEARCH_LIMIT)], cwd=repo)
    except GhError:
        return []
    return prs_naming(package, data)


def _group(primary: Alert, listing: object, number: str) -> tuple[list[Alert], list[str]]:
    """`primary` and its open siblings in the same manifest that have a patched version, and a warning for each
    sibling left out because it has none (no upgrade can clear it)."""
    from agentflow.gitops import GhError

    if not isinstance(listing, list):
        raise GhError(f"gh api {ALERTS_API} printed no list of alerts")
    others = []
    for entry in listing:
        dependency = entry.get("dependency") if isinstance(entry, dict) else None
        if not isinstance(dependency, dict) or dependency.get("manifest_path") != primary.manifest:
            continue  # another manifest's alert is never used, so its fields are never checked
        try:
            others.append(parse_alert(entry))
        except AlertError as exc:
            raise AlertError(f"Dependabot alert {_label(entry)}, open for the same package as #{number}: {exc}") from exc
    group, warnings, seen = [], [], set()
    for alert in siblings(primary, others):
        if alert.number in seen:
            continue
        seen.add(alert.number)
        if alert.patched is None:
            warnings.append(f"Dependabot alert #{alert.number} for {alert.package} has no patched version yet; "
                            "this run leaves it open")
            continue
        group.append(alert)
    return group, warnings


def _alert_text(group: list[Alert], ids: list[str], severity: str) -> str:
    from agentflow.intake import DATA_NOTE

    primary = group[0]
    lines = [DATA_NOTE, "",
             f"# Dependabot alert #{primary.number}: {primary.package} ({primary.ecosystem}) in {primary.manifest}", "",
             f"- Alerts: {', '.join(f'#{a.number}' for a in group)}",
             f"- Advisories: {', '.join(ids)}",
             f"- Severity: {severity}",
             f"- Package: {primary.package} ({primary.ecosystem})",
             f"- Manifest: {primary.manifest}"]
    for alert in group:
        lines += ["", f"## Alert #{alert.number} ({alert.severity}): {', '.join(alert.ids)}", "",
                  f"- Vulnerable: {alert.vulnerable_range}", f"- Patched in: {alert.patched}",
                  f"- URL: {_url(alert.url) or '(none)'}", "", "### Summary", "", alert.summary or "(none)", "",
                  "### Description", "", alert.description or "(none)"]
        if alert.references:
            lines += ["", "### References", "", *(f"- {ref}" for ref in alert.references)]
    return "\n".join(lines) + "\n"


def gather_alert(policy: VulnPolicy, ref: object, repo: Path, *, fork: str) -> tuple[dict, dict[str, bytes], list[str]]:
    """`(params_update, files, warnings)` for `--alert <ref>`, read through `gh` before any run exists.

    `params_update` holds `alert` (the number), `slug` (`<package>-<first GHSA id>`) and `advisory_ids` (the union of
    the group's ids); `files` are `alert.json` (the validated facts) and `alert.md` (the advisory text, as data);
    `warnings` are lines for the CLI. A refusal raises `ValueError` (an `AlertError` for a bad field) or `GhError`.
    """
    from agentflow import gitops
    from agentflow.state import slugify

    if policy.source != "dependabot":
        raise ValueError('vuln.source is off ("" in the pack), so --alert can\'t start a run: set vuln.source: '
                         'dependabot, or fix the dependency by hand or with `agentflow run bug --task ...`')
    number = alert_number(ref)
    repo = Path(repo)
    data = _api(f"{ALERTS_API}/{number}", repo, number)
    try:
        primary = parse_alert(data)
    except AlertError as exc:
        raise AlertError(f"Dependabot alert #{number}: {exc}") from exc
    if primary.number != int(number):
        raise AlertError(f"gh printed alert #{primary.number} when asked for Dependabot alert #{number}")
    if primary.state != "open":
        state = primary.state if primary.state in _CLOSED_STATES else repr(primary.state[:40])
        raise ValueError(f"Dependabot alert #{number} is {state}: only an open alert starts a vuln run")
    if primary.patched is None:
        raise ValueError(f"Dependabot alert #{number} ({primary.package}) has no patched version yet, so no upgrade "
                         "can fix it. A human can wait for a patched release, replace the package, or dismiss the "
                         "alert in GitHub.")
    if not severity_at_least(primary.severity, policy.min_severity):
        raise ValueError(f"Dependabot alert #{number} is {primary.severity}, below vuln.min_severity "
                         f"({policy.min_severity})")
    sha = gitops.commit_at(repo, f"refs/heads/{fork}") if fork else None
    if sha is None or gitops.read_blob(repo, sha, primary.manifest, MANIFEST_MAX) is None:
        raise ValueError(f"the alert's manifest {primary.manifest} is not a file at project.fork_from ({fork}): the "
                         "alert is for another branch, or the manifest moved")
    query = urllib.parse.urlencode({"state": "open", "package": primary.package, "ecosystem": primary.ecosystem,
                                    "per_page": 100}, quote_via=urllib.parse.quote, safe="")
    group, warnings = _group(primary, _api(f"{ALERTS_API}?{query}", repo, number), number)
    ids = list(dict.fromkeys(i for alert in group for i in alert.ids))
    severity = max((a.severity for a in group), key=SEVERITIES.index)
    prs = _dependabot_prs(primary.package, repo)
    warnings += [f"Dependabot PR #{n} is open for {primary.package}; this run's PR will supersede it" for n in prs]
    facts = {
        "source": "dependabot", "alert": primary.number, "alerts": [a.number for a in group],
        "ecosystem": primary.ecosystem,
        "package": primary.package, "manifest": primary.manifest, "severity": severity, "ids": ids,
        "advisories": [{"number": a.number, "severity": a.severity, "ids": list(a.ids),
                        "vulnerable_range": a.vulnerable_range, "patched": a.patched, "url": _url(a.url)}
                       for a in group],
        "dependabot_prs": prs,
    }
    files = {ALERT_JSON: (json.dumps(facts, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
             ALERT_FILE: _alert_text(group, ids, severity).encode("utf-8")}
    params = {"alert": number, "slug": slugify(f"{primary.package}-{primary.ids[0]}"), "advisory_ids": ids}
    return params, files, warnings


# --------------------------------------------------------------------------- intake: `--advisory ID`

ADVISORIES_API = "advisories"  # GitHub's global advisory database: public, so `gh` needs no token permission for it


def _fetch_advisories(ghsa_or_cve: str, repo: Path) -> list[Advisory]:
    """The global advisories for one id: a GHSA id is one `GET advisories/<id>`, a CVE id is one listing filtered by
    `cve_id`. The id was checked by `advisory_id`, and the query is encoded like an alert listing's."""
    from agentflow.gitops import GhError, gh_json

    if CVE_ID.fullmatch(ghsa_or_cve):
        query = urllib.parse.urlencode({"cve_id": ghsa_or_cve, "per_page": 100}, quote_via=urllib.parse.quote, safe="")
        data = gh_json(["api", f"{ADVISORIES_API}?{query}"], cwd=repo)
        if not isinstance(data, list):
            raise GhError(f"gh api {ADVISORIES_API} printed no list of advisories")
        found = [parse_advisory(entry) for entry in data]
        found = [a for a in found if a.cve == ghsa_or_cve]
    else:
        try:
            data = gh_json(["api", f"{ADVISORIES_API}/{ghsa_or_cve}"], cwd=repo)
        except GhError as exc:
            if "HTTP 404" in str(exc):
                raise ValueError(f"no GitHub advisory {ghsa_or_cve}") from exc
            raise
        found = [parse_advisory(data)]
        if found[0].ghsa != ghsa_or_cve:
            raise AlertError(f"gh printed advisory {found[0].ghsa} when asked for {ghsa_or_cve}")
    if not found:
        raise ValueError(f"no GitHub advisory {ghsa_or_cve}")
    return found


def _advisory_text(kept: list[tuple[Advisory, list[Vulnerability]]], ids: list[str], severity: str, package: str,
                   ecosystem: str, manifest: str) -> str:
    from agentflow.intake import DATA_NOTE

    lines = [DATA_NOTE, "", f"# Advisory {kept[0][0].ghsa}: {package} ({ecosystem}) in {manifest}", "",
             f"- Advisories: {', '.join(ids)}",
             f"- Severity: {severity}",
             f"- Package: {package} ({ecosystem})",
             f"- Manifest: {manifest}"]
    for advisory, hits in kept:
        lines += ["", f"## Advisory {advisory.ghsa} ({advisory.severity}): "
                      f"{', '.join(i for i in (advisory.ghsa, advisory.cve) if i)}", ""]
        lines += [f"- Vulnerable: {v.vulnerable_range}\n- Patched in: {v.patched}" for v in hits]
        lines += [f"- URL: {advisory.url or '(none)'}", "", "### Summary", "", advisory.summary or "(none)", "",
                  "### Description", "", advisory.description or "(none)"]
        if advisory.references:
            lines += ["", "### References", "", *(f"- {ref}" for ref in advisory.references)]
    return "\n".join(lines) + "\n"


def gather_advisory(policy: VulnPolicy, ref: object, package: object, manifest: object, repo: Path, *,
                    fork: str) -> tuple[dict, dict[str, bytes], list[str]]:
    """`(params_update, files, warnings)` for `--advisory <ref> --package <package> --manifest <manifest>`, read from
    GitHub's global advisory database through `gh` before any run exists. It keeps `gather_alert`'s contract.

    `params_update` holds `advisory` (the id), `slug` (`<package>-<first GHSA id>`) and `advisory_ids`; `files` are
    `alert.json` (the validated facts, `source: "advisory"`, `alert: null`, `alerts: []`) and `alert.md` (the
    advisories' text, as data). The advisory's text is never in the facts, the pull request or an argv. `vuln.source`
    governs Dependabot intake only, so it isn't checked here. A refusal raises `ValueError` (an `AlertError` for a bad
    field) or `GhError`.
    """
    from agentflow import gitops
    from agentflow.state import slugify

    wanted = advisory_id(ref)
    if package in (None, "") or manifest in (None, ""):
        raise ValueError("--advisory needs --package NAME and --manifest PATH")
    name, path = _package(str(package)), _manifest(str(manifest))
    repo = Path(repo)
    sha = gitops.commit_at(repo, f"refs/heads/{fork}") if fork else None
    if sha is None or gitops.read_blob(repo, sha, path, MANIFEST_MAX) is None:
        raise ValueError(f"the manifest {path} is not a file at project.fork_from ({fork}): the manifest is on "
                         "another branch, or it moved")
    found = _fetch_advisories(wanted, repo)
    live, _ = match_package(found, name)
    if not live:
        withdrawn = any(a.withdrawn and any(same_package(v.ecosystem, v.package, name) for v in a.vulnerabilities)
                        for a in found)
        if withdrawn:
            raise ValueError(f"GitHub advisory {wanted} is withdrawn, so no run can start from it")
        raise ValueError(f"GitHub advisory {wanted} does not name the package {name}")
    matches = [(a, match_package([a], name)[1]) for a in live]
    ecosystems = list(dict.fromkeys(v.ecosystem for _, hits in matches for v in hits))
    if len(ecosystems) > 1:
        raise ValueError(f"GitHub advisory {wanted} names {name} in more than one ecosystem "
                         f"({', '.join(ecosystems)}): this run takes one")
    warnings: list[str] = []
    kept: list[tuple[Advisory, list[Vulnerability]]] = []
    for advisory, hits in matches:
        patched = [v for v in hits if v.patched is not None]
        warnings += [f"GitHub advisory {advisory.ghsa} names {v.package} {v.vulnerable_range} with no patched "
                     "version yet; this run leaves it open" for v in hits if v.patched is None]
        if patched:
            kept.append((advisory, patched))
    if not kept:
        raise ValueError(f"GitHub advisory {wanted} ({name}) has no patched version yet, so no upgrade can fix it. "
                         "A human can wait for a patched release, replace the package, or fix it by hand.")
    known = [a.severity for a, _ in kept if a.severity in SEVERITIES]
    if not known:
        raise ValueError(f"GitHub advisory {wanted} has severity unknown: it is not rated, so the run can't judge "
                         f"it against vuln.min_severity ({policy.min_severity})")
    severity = max(known, key=SEVERITIES.index)
    if not severity_at_least(severity, policy.min_severity):
        raise ValueError(f"GitHub advisory {wanted} is {severity}, below vuln.min_severity ({policy.min_severity})")
    package_name, ecosystem = kept[0][1][0].package, kept[0][1][0].ecosystem
    ghsas = list(dict.fromkeys(a.ghsa for a, _ in kept))
    ids = ghsas + list(dict.fromkeys(a.cve for a, _ in kept if a.cve and a.cve not in ghsas))
    prs = _dependabot_prs(package_name, repo)
    warnings += [f"Dependabot PR #{n} is open for {package_name}; this run's PR will supersede it" for n in prs]
    facts = {
        "source": "advisory", "alert": None, "alerts": [], "ecosystem": ecosystem, "package": package_name,
        "manifest": path, "severity": severity, "ids": ids,
        "advisories": [{"number": None, "severity": a.severity, "ids": [i for i in (a.ghsa, a.cve) if i],
                        "vulnerable_range": v.vulnerable_range, "patched": v.patched, "url": a.url}
                       for a, hits in kept for v in hits],
        "dependabot_prs": prs,
    }
    files = {ALERT_JSON: (json.dumps(facts, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
             ALERT_FILE: _advisory_text(kept, ids, severity, package_name, ecosystem, path).encode("utf-8")}
    params = {"advisory": wanted, "slug": slugify(f"{package_name}-{ghsas[0]}"), "advisory_ids": ids}
    return params, files, warnings


def read_alert(run_dir: Path) -> dict:
    """The facts `gather_alert` kept in `<run_dir>/alert.json`. `AlertError` when the file is missing, unreadable, or
    names no advisory id: a vuln step must never read "no ids" as "nothing found"."""
    path = Path(run_dir) / ALERT_JSON
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AlertError(f"{path} could not be read: {exc}") from exc
    ids = data.get("ids") if isinstance(data, dict) else None
    if (not isinstance(ids, list) or not ids
            or not all(isinstance(i, str) and (GHSA_ID.fullmatch(i) or CVE_ID.fullmatch(i)) for i in ids)):
        raise AlertError(f"{path} names no advisory id")
    return data


# --------------------------------------------------------------------------- the pushed head: `vuln.dependency_review`

REVIEW_API = "repos/{owner}/{repo}/dependency-graph/compare"  # `gh api` fills in `{owner}` and `{repo}` itself
REVIEW_STOPS = ("new_advisory", "dependency_review_failed")  # the stops `review_dependencies` names; a person's approval accepts the head
_FULL_SHA1 = re.compile(r"^[0-9a-f]{40}$")
HITS_SHOWN = 10  # advisories a `new_advisory` summary names before `and <n> more`


def _accepted_by(events: list[dict], review: dict) -> str | None:
    """The person who accepted the failing `review`: the first approval on its key after it, when that approves a
    dependency-review stop, is not the runner's own (`AUTO_BY`) and is not a retry's. `run_control.retry` clears the
    stop it retries with a `hitl_approved` and then, under the same lock, a `step_retry` on the key, so an approval
    the key's next event retries asked GitHub again rather than accepted the head; a `step_retry` before any approval
    means the same. None when no one accepted it."""
    from agentflow.autonomy import AUTO_BY  # it imports `config`, which imports this module

    after = [e for e in events if e.get("seq", -1) > review.get("seq", -1) and e.get("key") == review.get("key")]
    for index, event in enumerate(after):
        if event["type"] == "step_retry":
            return None
        if event["type"] == "hitl_approved":
            retried = index + 1 < len(after) and after[index + 1]["type"] == "step_retry"
            by = str(event.get("by") or "")
            return by if event.get("kind") in REVIEW_STOPS and by and by != AUTO_BY and not retried else None
    return None


def _reviews(events: list[dict], head: str, key: str | None = None) -> list[dict]:
    return [e for e in events if e.get("type") == "dependency_reviewed" and head and e.get("head") == head
            and (key is None or e.get("key") == key)]


def _hit_text(hit: dict) -> str:
    return f"{hit['ghsa']} ({hit['severity']}) in {hit['package']} {hit['version']} ({hit['manifest']})"


def review_dependencies(cfg: Config, run: Run, worktree: Path, key: str, head: str) -> tuple[str, str]:
    """GitHub's dependency review of the pushed `head` against `origin/<fork_from>`: `(verdict, summary)`.

    `off` when `vuln.dependency_review` is false or `run` is not a vuln run (nothing is asked or recorded); `accepted`
    when a person approved the stop a failing review of this same `head` raised on `key` (nothing is asked again);
    otherwise one `gh api <REVIEW_API>/<base>...<head>` (a GET: no fields; both full 40-hex shas, checked before `gh` sees them),
    read by `review_changes` against the run's package (`alert.json`) and `vuln.min_severity`: `ok`, `new_advisory`
    when the head adds an advisory at or above it, or `dependency_review_failed` for a `gh` error, a reply that is not
    the documented shape or shows no change to the package yet, or a sha that cannot be read. The check fails closed:
    nothing but `ok` and `accepted` lets the pull request be marked ready. Each verdict but `off` and `accepted` is a
    `dependency_reviewed` event. Text from GitHub reaches the summary and the event only as `review_changes`' validated
    fields or a `gh` error, and each passes `hitl.clean_fact`."""
    from agentflow.gitops import GhError, commit_at, gh_json
    from agentflow.hitl import clean_fact

    if not cfg.vuln.dependency_review or run.state.get("pipeline") != "vuln":
        return "off", ""
    events = run.events.read()
    mine = _reviews(events, head, key)
    if mine and mine[-1].get("ok") is not True:
        by = _accepted_by(events, mine[-1])
        if by is not None:
            return "accepted", clean_fact(f"{by} accepted the dependency review of {head[:12]}.")
    floor = cfg.vuln.min_severity
    fork = cfg.project.fork_from
    base = commit_at(worktree, f"refs/remotes/origin/{fork}") or ""
    hits: list[dict] = []
    error = ""
    if not _FULL_SHA1.fullmatch(head or ""):
        error = "the pushed head is not a full 40-character commit id"
    elif not _FULL_SHA1.fullmatch(base):
        error = f"origin/{fork} is not a commit with a full 40-character id in the worktree"
    else:
        try:
            alert = read_alert(run.dir)
            reply = gh_json(["api", f"{REVIEW_API}/{base}...{head}"], cwd=worktree)
            hits = review_changes(reply, floor, str(alert.get("package") or ""), str(alert.get("ecosystem") or ""))
        except (GhError, AlertError) as exc:
            error = clean_fact(str(exc))
    hits = [{field: clean_fact(str(hit[field])) for field in ("ghsa", "severity", "package", "version", "manifest")}
            for hit in hits]
    short = head[:12] if _FULL_SHA1.fullmatch(head or "") else "the pushed head"
    if error and not _FULL_SHA1.fullmatch(head or ""):  # no head to pin an approval to: approving only stops again
        verdict = "dependency_review_failed"
        summary = (f"The pushed head is unknown (the run recorded no full commit id for its last push), so GitHub's "
                   f"dependency review cannot be asked and `agentflow approve` cannot accept it: `agentflow cancel "
                   f"{run.id}`, or set `vuln.dependency_review: false`.")
    elif error:
        verdict = "dependency_review_failed"
        summary = f"GitHub's dependency review of {short} could not be read: {error}"
    elif hits:
        verdict = "new_advisory"
        more = f" and {len(hits) - HITS_SHOWN} more" if len(hits) > HITS_SHOWN else ""
        summary = (f"The pushed head {short} adds {len(hits)} advisory(ies) at or above {floor}: "
                   f"{'; '.join(_hit_text(h) for h in hits[:HITS_SHOWN])}{more}.")
    else:
        verdict = "ok"
        summary = f"GitHub's dependency review of {short} names no new advisory at or above {floor}."
    run.emit("dependency_reviewed", key=key, head=head if _FULL_SHA1.fullmatch(head or "") else "", base=base,
             floor=floor, ok=verdict == "ok", verdict=verdict, advisories=hits, error=error)
    return verdict, clean_fact(summary)


# --------------------------------------------------------------------------- the pull request: `finish`

TARGET_MAX = 64  # characters of each of the triage's versions (current, target) the pull request shows
SCOPE_SHOWN = 10  # paths the code-change line names before `and <n> more`


def code_change_paths(cfg: Config, changed: Sequence[str]) -> list[str]:
    """The paths of `changed` that match none of `vuln.dependency_files`: code the upgrade needed changed, which the
    pull request names (`vuln_scope`)."""
    from agentflow.guards import path_matches

    return [p for p in changed if not any(path_matches(p, glob) for glob in cfg.vuln.dependency_files)]


def _code(text: object) -> str:
    """`text` as inline code, with no backtick of its own that could close the span early."""
    return "`" + str(text).replace("`", "") + "`"


def _last_ok(events: list[dict], expect: str, before: int | None = None) -> dict | None:
    found = [e for e in events if e.get("type") == "expectation_checked" and e.get("expect") == expect
             and e.get("ok") is True and not e.get("forbidden") and (before is None or e["seq"] < before)]
    return found[-1] if found else None


def _probe_line(events: list[dict], pushed_tree: str) -> str:
    """`Probe: present (<ids>) → cleared` from the last passing cleared check on `pushed_tree` (the tree of the head
    the pull request shows) and the last passing present check before it. A passing pair on another tree only (a CI
    fix or a re-sync changed the head since) is `Probe: not re-checked on the pushed head`; no passing pair at all is
    `Probe: not cleared`. Only an id of the advisory patterns is shown."""
    from agentflow.config import ADVISORY_CLEARED, ADVISORY_PRESENT

    def pair(tree: str | None) -> tuple[dict, dict] | None:
        for cleared in reversed([e for e in events if e.get("type") == "expectation_checked"
                                 and e.get("expect") == ADVISORY_CLEARED and e.get("ok") is True
                                 and not e.get("forbidden") and (tree is None or e.get("tree") == tree)]):
            present = _last_ok(events, ADVISORY_PRESENT, cleared["seq"])
            if present is not None:
                return present, cleared
        return None

    found = pair(pushed_tree) if pushed_tree else None
    if found is None:
        return "Probe: not re-checked on the pushed head" if pair(None) else "Probe: not cleared"
    ids = [str(i) for i in found[0].get("matched") or [] if GHSA_ID.fullmatch(str(i)) or CVE_ID.fullmatch(str(i))]
    return f"Probe: present ({', '.join(ids)}) → cleared"


def _versions(facts: object) -> str:
    """`Target: <from> → <to>` from the triage's cleaned facts, each as inline code cut to `TARGET_MAX`, or `unknown`;
    `Target: unknown` when neither is known."""
    from agentflow.hitl import clean_fact

    def one(field: str) -> str:
        text = clean_fact(str(facts.get(field) or "") if isinstance(facts, dict) else "")[:TARGET_MAX]
        return _code(text) if text.replace("`", "").strip() else "unknown"

    old, new = one("current_version"), one("target_version")
    return "Target: unknown" if old == new == "unknown" else f"Target: {old} → {new}"


def _review_line(events: list[dict], pushed_sha: str) -> str:
    """The `Dependency review:` line, from the `dependency_reviewed` events alone ("" when there is none: the check is
    off, or has not run yet). The last review of `pushed_sha` (the head the body goes out with) says `no new advisory
    at or above <floor>`, or `accepted by <by> (<ids>)` when a person approved its stop; reviews of other heads only
    say `not checked on the pushed head`. Only GHSA ids and a known severity name are shown."""
    from agentflow.hitl import clean_fact

    if not any(e.get("type") == "dependency_reviewed" for e in events):
        return ""
    mine = _reviews(events, pushed_sha)
    if not mine:
        return "Dependency review: not checked on the pushed head"
    last = mine[-1]
    if last.get("ok") is True:
        floor = str(last.get("floor") or "")
        return f"Dependency review: no new advisory at or above {floor if floor in SEVERITIES else 'unknown'}"
    ids = list(dict.fromkeys(str(a.get("ghsa")) for a in last.get("advisories") or []
                             if isinstance(a, dict) and GHSA_ID.fullmatch(str(a.get("ghsa")))))
    named = ", ".join(ids) or "unchecked"
    by = _accepted_by(events, last)
    if by is not None:
        return f"Dependency review: accepted by {clean_fact(by)[:100]} ({named})"
    if last.get("verdict") == "new_advisory":
        return f"Dependency review: new advisory on the pushed head ({named})"
    return "Dependency review: could not be read on the pushed head"


def pr_lines(run: Run, pushed_tree: str = "", pushed_sha: str = "") -> list[str]:
    """The lines of the pull request's `Security advisory` section; [] for a run with no readable `alert.json`.

    They come from the facts intake validated (`alert.json`), the probe's `expectation_checked` events (`cleared`
    only for a check on `pushed_tree`, the tree of the head the body goes out with; "" never shows it), the triage's
    cleaned facts (`triage_rendered`, the current and target versions as inline code, each cut to `TARGET_MAX`) and
    the last `vuln_scope`, and the `dependency_reviewed` events (`_review_line`, for `pushed_sha`, the head's commit).
    The advisory's summary and description never go in, and each line passes `hitl.clean_fact`, so no
    text from outside the run or from a model can start a heading, a list item or a line of its own."""
    from agentflow.hitl import clean_fact

    try:
        alert = read_alert(run.dir)
    except AlertError:
        return []
    events = run.events.read()
    advisories = [a for a in alert.get("advisories") or [] if isinstance(a, dict)]
    lines = [f"Advisories: {', '.join(i for i in alert['ids'])}"]
    severity = str(alert.get("severity") or "")
    lines.append(f"Severity: {severity if severity in SEVERITIES else 'unknown'}")
    ecosystem = str(alert.get("ecosystem") or "")
    lines.append(f"Package: {_code(alert.get('package') or '')} ({ecosystem if ecosystem in ECOSYSTEMS else 'unknown'}) in "
                 f"{_code(alert.get('manifest') or '')}")
    ranges = dict.fromkeys((str(a.get("vulnerable_range") or "?"), str(a.get("patched") or "?")) for a in advisories)
    lines += [f"Vulnerable: {vulnerable} · patched in {patched}" for vulnerable, patched in ranges]
    triage = [e for e in events if e.get("type") == "triage_rendered"]
    lines.append(_versions((triage[-1].get("facts") if triage else None) or {}))
    label = "Advisory" if alert.get("source") == "advisory" else "Alert"  # an advisory run has no alert number
    shown: set[str] = set()
    for advisory in advisories:
        url, number = _url(str(advisory.get("url") or "")), advisory.get("number")
        if url:
            text = f"{label}: {url}"
        elif isinstance(number, int) and not isinstance(number, bool) and number > 0:
            text = f"{label}: #{number}"
        else:
            continue
        if text not in shown:  # one advisory can name the package twice
            shown.add(text)
            lines.append(text)
    lines += [f"Supersedes #{n} (Dependabot)" for n in alert.get("dependabot_prs") or []
              if isinstance(n, int) and not isinstance(n, bool) and n > 0]
    lines.append(_probe_line(events, pushed_tree))
    review = _review_line(events, pushed_sha)
    if review:
        lines.append(review)
    scopes = [e for e in events if e.get("type") == "vuln_scope"]
    paths = [str(p) for p in (scopes[-1].get("code_paths") or [] if scopes else [])]
    if paths:
        more = f" and {len(paths) - SCOPE_SHOWN} more" if len(paths) > SCOPE_SHOWN else ""
        lines.append(f"Code changed outside dependency files: {', '.join(_code(p) for p in paths[:SCOPE_SHOWN])}{more}")
    return [clean_fact(line) for line in lines]
