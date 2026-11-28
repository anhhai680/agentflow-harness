"""Dependency facts from a manifest: `pyproject.toml`, `requirements*.txt` or `package.json`, as `graph_model.Facts`.

`extract` picks the parser by the file's name. `tomllib` and `json` read the data, and nothing in it is run, resolved or
fetched. It never raises: a file that does not decode, parse or have the expected shape gives
`empty("manifest", <reason>)`. A dependency whose name `graph_model.module` would refuse is dropped and counted in a
note, and the list is cut to `MAX_DEPS` with a note, so the facts are always accepted.

A Python name is normalised as PEP 503 does (lower case, each run of `-`, `_` and `.` as one `-`). Its `spec` is the version
part of the requirement: the extras and the environment marker are left out.

The worker process imports this module, so it uses the standard library and `graph_model` only.
"""

from __future__ import annotations

import json
import re
import tomllib

from agentflow.graph_model import MAX_DEPS, NAME_MAX, Dep, Facts, empty, module, text

_REQUIREMENT = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(.*)", re.DOTALL)
_SEPARATORS = re.compile(r"[-_.]+")
_SPEC_STARTS = ("<", ">", "=", "!", "~", "(", "@")
_NPM_GROUPS = (("dependencies", "runtime"), ("devDependencies", "dev"), ("optionalDependencies", "optional"), ("peerDependencies", "optional"))


def extract(path: str, data: bytes) -> Facts:
    """The dependencies a manifest declares. Pure; never raises.

    `path` picks the parser by its last segment (`pyproject.toml`, `package.json` or `requirements*.txt`) and is not read.
    """
    try:
        name = path.rsplit("/", 1)[-1]
        if name == "pyproject.toml":
            return _pyproject(data)
        if name == "package.json":
            return _package_json(data)
        if name.startswith("requirements") and name.endswith(".txt"):
            return _requirements(name, data)
        return empty("manifest", "not a supported manifest file name")
    except Exception as exc:  # the contract is "never raises": whatever a parser bug throws is one note
        return empty("manifest", type(exc).__name__)


class _Deps:
    """The dependencies found so far, with the ones left out counted for one note."""

    def __init__(self) -> None:
        self.rows: list[Dep] = []
        self.dropped = 0
        self.cut = False

    def add(self, name: str, spec: str, group: str) -> None:
        clean = module(name)
        if clean is None:
            self.dropped += 1
        elif len(self.rows) >= MAX_DEPS:
            self.cut = True
        else:
            self.rows.append(Dep(clean, text(spec, NAME_MAX), group))

    def add_requirement(self, line: str, group: str) -> None:
        parsed = _requirement(line)
        if parsed is None:
            self.dropped += 1
        else:
            self.add(parsed[0], parsed[1], group)

    def notes(self) -> list[str]:
        out = []
        if self.dropped:
            out.append(f"dropped {self.dropped} dependencies with names outside [A-Za-z0-9_@./-] or that are not requirements")
        if self.cut:
            out.append(f"cut dependencies to {MAX_DEPS}")
        return out


def _package_name(raw: object, notes: list[str]) -> str:
    if raw is None:
        return ""
    clean = module(raw)
    if clean is None:
        notes.append("dropped a package name outside [A-Za-z0-9_@./-]")
        return ""
    return clean


# --- pyproject.toml ---


def _pyproject(data: bytes) -> Facts:
    try:
        doc = tomllib.loads(data.decode("utf-8-sig"))
    except (ValueError, RecursionError) as exc:  # a TOML error and a bad UTF-8 byte are both ValueErrors
        return empty("manifest", f"pyproject.toml cannot be read ({type(exc).__name__})")
    if "project" not in doc:
        return Facts(lang="manifest", ecosystem="pypi", notes=("pyproject.toml has no [project] table",))
    project = doc["project"]
    if not isinstance(project, dict):
        return empty("manifest", "[project] is not a table")
    runtime = project.get("dependencies", [])
    optional = project.get("optional-dependencies", {})
    if not isinstance(runtime, list):
        return empty("manifest", "[project] dependencies is not a list")
    if not isinstance(optional, dict) or not all(isinstance(group, list) for group in optional.values()):
        return empty("manifest", "[project.optional-dependencies] is not a table of lists")
    deps = _Deps()
    for group, entries in (("runtime", runtime), *(("optional", entries) for entries in optional.values())):
        for entry in entries:
            if isinstance(entry, str):
                deps.add_requirement(entry, group)
            else:
                deps.dropped += 1
    notes: list[str] = []
    raw_name = project.get("name")
    package = _package_name(_normalise(raw_name) if isinstance(raw_name, str) else None, notes)
    return Facts(lang="manifest", deps=tuple(deps.rows), package=package, ecosystem="pypi", notes=tuple(notes + deps.notes()))


def _normalise(name: str) -> str:
    return _SEPARATORS.sub("-", name.strip()).lower()


def _requirement(line: str) -> tuple[str, str] | None:
    """The normalised name and the version part of one PEP 508 requirement, or None when it is not one."""
    found = _REQUIREMENT.match(line.strip())
    if found is None:
        return None
    spec = " ".join(found.group(2).split(";", 1)[0].split()).split(" --", 1)[0].strip()
    if spec and not spec.startswith(_SPEC_STARTS):
        return None
    if spec.startswith("(") and spec.endswith(")"):
        spec = spec[1:-1].strip()
    return _normalise(found.group(1)), spec


# --- requirements*.txt ---


def _requirements(file_name: str, data: bytes) -> Facts:
    group = "dev" if "dev" in file_name or "test" in file_name else "runtime"
    deps = _Deps()
    joined = ""
    for raw in data.decode("utf-8-sig").splitlines():
        line = joined + raw.replace("\t", " ")
        if line.endswith("\\"):  # a continued line, as pip reads it
            joined = line[:-1] + " "
            continue
        joined = ""
        line = line.split(" #", 1)[0].strip()
        if line and not line.startswith(("#", "-")):
            deps.add_requirement(line, group)
        if deps.cut:
            break
    return Facts(lang="manifest", deps=tuple(deps.rows), ecosystem="pypi", notes=tuple(deps.notes()))


# --- package.json ---


def _package_json(data: bytes) -> Facts:
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except (ValueError, RecursionError) as exc:
        return empty("manifest", f"package.json cannot be read ({type(exc).__name__})")
    if not isinstance(doc, dict):
        return empty("manifest", "package.json is not a JSON object")
    sections = [(doc.get(key), group, key) for key, group in _NPM_GROUPS if key in doc]
    for table, _group, key in sections:
        if not isinstance(table, dict):
            return empty("manifest", f"package.json {key} is not an object")
    deps = _Deps()
    for table, group, _key in sections:
        for name, spec in table.items():
            if isinstance(spec, str):
                deps.add(name, spec, group)
            else:
                deps.dropped += 1
    notes: list[str] = []
    raw_name = doc.get("name")
    package = _package_name(raw_name if isinstance(raw_name, str) else None, notes)
    return Facts(lang="manifest", deps=tuple(deps.rows), package=package, ecosystem="npm", notes=tuple(notes + deps.notes()))
