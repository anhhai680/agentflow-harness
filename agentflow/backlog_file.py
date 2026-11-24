"""Parse and validate `backlog.yml`, the file that holds every backlog item (spec: agentflow-backlog-file-proposal).

`parse` and `read` never raise. A file-level fault (not YAML, a wrong `version`, `items` not a list, too many items)
gives no items and one error. An item-level error drops only that item, except a bad `spec` or `plan` path, which is
cleared and the item stays. An unknown key is a warning. Whether a `spec` exists, a lane is the pack's, or a
dependency is known is `validate`'s business, not the parser's."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from . import safeio, sources

STATUSES = ("backlog", "ready", "done", "dropped")
FILE_MAX = 1_000_000
ITEMS_MAX = 2000
KEYS = ("id", "title", "lane", "status", "spec", "plan", "depends_on", "issue", "notes")
ID_MAX = 64
TITLE_MAX = 200
NOTES_MAX = 4096
FILE = "backlog.yml"


@dataclass
class BacklogItem(sources.ModuleNode):
    """A module node (`deps` is `depends_on`) with the Board's fields."""

    title: str = ""
    lane: str = ""
    status: str = "backlog"
    spec: str = ""
    plan: str = ""
    issue: int | None = None
    notes: str = ""


@dataclass(frozen=True)
class Problem:
    level: Literal["error", "warning"]
    where: str  # `items[3].lane`, or `backlog.yml` for the whole file
    message: str


class _ItemError(Exception):
    def __init__(self, key: str, message: str):
        super().__init__(message)
        self.key = key
        self.message = message


def _bad_path(value: str) -> bool:
    """True for an absolute path or one with a `..` segment once normalised."""
    if not value or value.startswith("/") or "\\" in value or "\0" in value:
        return True
    norm = posixpath.normpath(value)
    return norm.startswith("/") or ".." in norm.split("/")


def _depends(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list) and all(isinstance(entry, str) for entry in value):
        return [entry.strip() for entry in value if entry.strip()]
    raise _ItemError("depends_on", "must be a list of item ids (or one comma-separated string)")


def _item(raw: dict, problems: list[Problem], at: str) -> BacklogItem:
    """The item for one mapping, or raise `_ItemError` for the first error. A bad path is a problem, not a drop."""
    for key in raw:
        if key not in KEYS:
            problems.append(Problem("warning", f"{at}.{key}", f"unknown key {key!r}: the engine does not read it"))
    ident = raw.get("id")
    if not isinstance(ident, str) or len(ident) > ID_MAX or sources.MODULE_ID.fullmatch(ident) is None:
        raise _ItemError("id", f"must be a kebab-case id of at most {ID_MAX} characters")
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise _ItemError("title", "is required: a one-line string")
    if "\n" in title or "\r" in title or len(title) > TITLE_MAX:
        raise _ItemError("title", f"must be one line of at most {TITLE_MAX} characters")
    status = raw.get("status", "backlog")
    if status not in STATUSES:
        raise _ItemError("status", f"must be one of {', '.join(STATUSES)}")
    lane = raw.get("lane", "")
    if lane is None:
        lane = ""
    if not isinstance(lane, str):
        raise _ItemError("lane", "must be a string")
    paths = {}
    for key in ("spec", "plan"):
        value = raw.get(key, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise _ItemError(key, "must be a string")
        paths[key] = value
    deps = _depends(raw.get("depends_on"))
    issue = raw.get("issue")
    if issue is not None and (isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0):
        raise _ItemError("issue", "must be a positive integer")
    notes = raw.get("notes", "")
    if notes is None:
        notes = ""
    if not isinstance(notes, str) or len(notes) > NOTES_MAX:
        raise _ItemError("notes", f"must be a string of at most {NOTES_MAX} characters")
    for key, value in paths.items():
        if value and _bad_path(value):
            problems.append(Problem("error", f"{at}.{key}", f"{value!r} must be a path inside the repository"))
            paths[key] = ""
    return BacklogItem(
        ident, deps, title=title, lane=lane, status=status, spec=paths["spec"], plan=paths["plan"], issue=issue, notes=notes
    )


def parse(text: str) -> tuple[list[BacklogItem], list[Problem]]:
    """The items of a backlog file's text and every problem found. Never raises."""

    def broken(where: str, message: str) -> tuple[list[BacklogItem], list[Problem]]:
        return [], [Problem("error", where, message)]

    try:
        data = yaml.safe_load(text.removeprefix("﻿"))
    except (yaml.YAMLError, RecursionError, ValueError) as exc:
        return broken(FILE, f"is not valid YAML: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}")
    if not isinstance(data, dict):
        return broken(FILE, "must be a mapping with `version` and `items`")
    version = data.get("version")
    if isinstance(version, bool) or version != 1 or not isinstance(version, int):
        return broken("version", "must be the integer 1")
    raw_items = data.get("items")
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, list):
        return broken("items", "must be a list")
    if len(raw_items) > ITEMS_MAX:
        return broken("items", f"has {len(raw_items)} entries; the limit is {ITEMS_MAX}")
    items: list[BacklogItem] = []
    problems: list[Problem] = []
    first: dict[str, int] = {}
    for n, raw in enumerate(raw_items):
        at = f"items[{n}]"
        if not isinstance(raw, dict):
            problems.append(Problem("error", at, "must be a mapping"))
            continue
        found: list[Problem] = []
        try:
            item = _item({str(k): v for k, v in raw.items()}, found, at)
        except _ItemError as exc:
            problems.append(Problem("error", f"{at}.{exc.key}", exc.message))
            continue
        if item.id in first:
            problems.append(Problem("error", f"{at}.id", f"duplicate id {item.id} (first at items[{first[item.id]}])"))
            continue
        first[item.id] = n
        problems.extend(found)
        items.append(item)
    return items, problems


def read(path: Path) -> tuple[list[BacklogItem], list[Problem]]:
    """`parse` of the file at `path`, read without following a symlink. Never raises."""
    try:
        data = safeio.read_bytes(path, FILE_MAX + 1)
    except safeio.SymlinkRefused:
        return [], [Problem("error", FILE, "is a symlink: the engine does not read it")]
    except (OSError, ValueError) as exc:
        return [], [Problem("error", FILE, f"cannot be read: {exc}")]
    if data is None:
        return [], [Problem("error", FILE, "missing: agentflow backlog import writes it")]
    if len(data) > FILE_MAX:
        return [], [Problem("error", FILE, f"is larger than {FILE_MAX} bytes")]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [], [Problem("error", FILE, "is not valid UTF-8")]
    return parse(text)
