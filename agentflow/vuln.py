"""The vuln lane's alert model, its validation and the probe verdict: pure functions, no I/O, no process.

Trust model. A Dependabot alert is data from outside the run: GitHub's reply carries text that other people wrote
(package names, advisory summaries, version ranges). `parse_alert` is the one place that turns it into `Alert`, and it
refuses the whole alert when a field the runner *uses* (the ecosystem, package, manifest path, versions, advisory ids)
doesn't match its pattern. A refusal is an `AlertError`, never a repair. The free text (`summary`, `description`,
`references`) is kept only to show to the agent, capped in size, and is never used as a command, a path or a gate.

`judge` is the verdict of the probe, the pack's own scanner command, on whether the alert's advisory is still there. A
scanner that dies must never read as a clean scan, so `judge` answers "failed" unless the probe ran cleanly.

`config.py` imports this module's constants at module level, so this module imports nothing from `agentflow` at run
time (`config`, `gates` and `state` would be a cycle).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

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
_VERSION = re.compile(r"^[0-9A-Za-z.+~_\- <>=!,^*]{1,64}$")
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
    if not _PACKAGE.fullmatch(name) or not name.isprintable():
        raise AlertError("the package name is empty, too long, or has whitespace or shell characters")
    return name


def _manifest(path: str) -> str:
    pure = PurePosixPath(path)
    if (not path or "\x00" in path or "\\" in path or pure.is_absolute() or ".." in pure.parts
            or not path.isprintable()):
        raise AlertError("the manifest path must be relative, with no '..', and printable")
    return path


def _version(value: str, what: str) -> str:
    if not _VERSION.fullmatch(value):
        raise AlertError(f"{what} is not a version")
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
                                  "the vulnerable range"),
        patched=patched, url=str(root.get("html_url") or ""),
        summary=str(advisory.get("summary") or "")[:MAX_TEXT],
        description=str(advisory.get("description") or "")[:MAX_TEXT],
        references=_references(advisory))


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
