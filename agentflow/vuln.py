"""The vuln lane's alert model, its validation, its intake from GitHub and the probe verdict.

Everything but `gather_alert` and `read_alert` is a pure function. `gather_alert` reads a Dependabot alert through `gh`
(an argv list, never a shell) before any run exists, and `read_alert` reads back the facts it kept (`alert.json`).

Trust model. A Dependabot alert is data from outside the run: GitHub's reply carries text that other people wrote
(package names, advisory summaries, version ranges). `parse_alert` is the one place that turns it into `Alert`, and it
refuses the whole alert when a field the runner *uses* (the ecosystem, package, manifest path, versions, advisory ids)
doesn't match its pattern. A refusal is an `AlertError`, never a repair. The free text (`summary`, `description`,
`references`) is kept only to show to the agent, capped in size, and is never used as a command, a path or a gate.

`judge` is the verdict of the probe, the pack's own scanner command, on whether the alert's advisory is still there. A
scanner that dies must never read as a clean scan, so `judge` answers "failed" unless the probe ran cleanly.

`config.py` imports this module's constants at module level, so this module imports nothing from `agentflow` at
module level (`config`, `gates` and `state` would be a cycle): `gather_alert` imports `gitops`, `intake` and `state`
when it runs, and `run_probe` imports `config`, `gates` and `guards`.
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
    if not _PACKAGE.fullmatch(name) or not name.isprintable() or name.startswith("-"):
        raise AlertError("the package name is empty, too long, starts with '-', or has whitespace or shell characters")
    return name


def _manifest(path: str) -> str:
    pure = PurePosixPath(path)
    if (not path or "\x00" in path or "\\" in path or pure.is_absolute() or ".." in pure.parts
            or not path.isprintable() or not pure.parts or posixpath.normpath(path) == "."):
        raise AlertError("the manifest path must name a file: relative, with no '..', and printable")
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


def _label(entry: object) -> str:
    number = entry.get("number") if isinstance(entry, dict) else None
    return f"#{number}" if isinstance(number, int) and not isinstance(number, bool) else "with no number"


def _url(text: str) -> str:
    """`text` when it is a plain https URL (no space, quote, bracket or markup), else ""."""
    return text if _URL.fullmatch(text) else ""


def _dependabot_prs(package: str, repo: Path) -> list[int]:
    """The open Dependabot PRs whose title names `package` as a whole token; [] when `gh` fails or prints junk."""
    from agentflow.gitops import GhError, gh_json

    try:
        data = gh_json(["pr", "list", "--author", "app/dependabot", "--state", "open", "--search", package,
                        "--json", "number,title,url", "--limit", str(PR_SEARCH_LIMIT)], cwd=repo)
    except GhError:
        return []
    token = re.compile(rf"(?<![\w.-]){re.escape(package)}(?![\w.-])", re.IGNORECASE)
    found: list[int] = []
    for pr in data if isinstance(data, list) else []:
        number, title = (pr.get("number"), pr.get("title")) if isinstance(pr, dict) else (None, None)
        if (isinstance(number, int) and not isinstance(number, bool) and number > 0 and isinstance(title, str)
                and token.search(title) and number not in found):
            found.append(number)
    return found


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
        "alert": primary.number, "alerts": [a.number for a in group], "ecosystem": primary.ecosystem,
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


# --------------------------------------------------------------------------- the pull request: `finish`

TARGET_MAX = 64  # characters of the triage's target version the pull request shows
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


def _probe_line(events: list[dict]) -> str:
    """`Probe: present (<ids>) → cleared` from the last passing cleared check and the last passing present check
    before it, else `Probe: not cleared`. Only an id of the advisory patterns is shown."""
    from agentflow.config import ADVISORY_CLEARED, ADVISORY_PRESENT

    cleared = _last_ok(events, ADVISORY_CLEARED)
    present = _last_ok(events, ADVISORY_PRESENT, cleared["seq"]) if cleared else None
    if present is None:
        return "Probe: not cleared"
    ids = [str(i) for i in present.get("matched") or [] if GHSA_ID.fullmatch(str(i)) or CVE_ID.fullmatch(str(i))]
    return f"Probe: present ({', '.join(ids)}) → cleared"


def pr_lines(run: Run) -> list[str]:
    """The lines of the pull request's `Security advisory` section; [] for a run with no readable `alert.json`.

    They come from the facts intake validated (`alert.json`), the probe's `expectation_checked` events, the triage's
    cleaned facts (`triage_rendered`, the target version as inline code, cut to `TARGET_MAX`) and the last
    `vuln_scope`. The advisory's summary and description never go in, and each line passes `hitl.clean_fact`, so no
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
    facts = (triage[-1].get("facts") if triage else None) or {}
    target = clean_fact(str(facts.get("target_version") or "") if isinstance(facts, dict) else "")[:TARGET_MAX]
    lines.append(f"Target: {_code(target)}" if target.replace("`", "").strip() else "Target: unknown")
    for advisory in advisories:
        url, number = _url(str(advisory.get("url") or "")), advisory.get("number")
        if url:
            lines.append(f"Alert: {url}")
        elif isinstance(number, int) and not isinstance(number, bool) and number > 0:
            lines.append(f"Alert: #{number}")
    lines += [f"Supersedes #{n} (Dependabot)" for n in alert.get("dependabot_prs") or []
              if isinstance(n, int) and not isinstance(n, bool) and n > 0]
    lines.append(_probe_line(events))
    scopes = [e for e in events if e.get("type") == "vuln_scope"]
    paths = [str(p) for p in (scopes[-1].get("code_paths") or [] if scopes else [])]
    if paths:
        more = f" and {len(paths) - SCOPE_SHOWN} more" if len(paths) > SCOPE_SHOWN else ""
        lines.append(f"Code changed outside dependency files: {', '.join(_code(p) for p in paths[:SCOPE_SHOWN])}{more}")
    return [clean_fact(line) for line in lines]
