"""Parse, render and edit `backlog.yml`, the file that holds every backlog item (spec: agentflow-backlog-file-proposal).

`parse` and `read` never raise. A file-level fault (not YAML, a wrong `version`, `items` not a list, too many items)
gives no items and one error. An item-level error drops only that item, except a bad `spec` or `plan` path, which is
cleared and the item stays. An unknown key is a warning. Whether a `spec` exists, a lane is the pack's, or a
dependency is known is `validate`'s business, not the parser's."""

from __future__ import annotations

import json
import posixpath
import re
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
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
PATH_MAX = 255  # characters of an item's `spec` or `plan`
# A control (C0, DEL, C1), format (bidi overrides, zero-width), surrogate or line/paragraph-separator character: a path
# that holds one could start a new line, or hide text, in the prompt `{spec}` reaches.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


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


def bad_path(value: str) -> bool:
    """True for a `spec` or `plan` an item can't name: empty, absolute, a `..` segment once normalised, a backslash, a
    leading `-` (git would read it as an option), more than `PATH_MAX` characters, or any control, format, surrogate or
    line/paragraph-separator character (the path reaches prompts as `{spec}`)."""
    if not value or len(value) > PATH_MAX or value.startswith(("/", "-")) or "\\" in value:
        return True
    if any(unicodedata.category(ch) in _UNSAFE_CATEGORIES for ch in value):
        return True
    norm = posixpath.normpath(value)
    return norm.startswith("/") or ".." in norm.split("/")


def path_problem(repo: Path, value: str) -> str | None:
    """Why the item path `value` can't be used inside `repo`: it is, or passes through, a symlink, it resolves outside
    `repo`, or it can't be checked (an unreadable directory or a symlink loop). None when it can; it need not exist.
    Only `lstat` and `resolve` look at the tree: nothing is read."""
    root = Path(repo)
    here = root
    try:
        for part in PurePosixPath(value).parts:
            here = here / part
            if here.is_symlink():
                return "is, or passes through, a symlink: the engine does not read it"
        if not here.resolve().is_relative_to(root.resolve()):
            return "resolves outside the repository"
    except (OSError, RuntimeError, ValueError):  # a symlink loop on the way (RuntimeError on Python 3.12), or unreadable
        return "cannot be checked: it is unreadable or loops"
    return None


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
        if value and bad_path(value):
            shown = repr(value[:80]) + ("…" if len(value) > 80 else "")
            problems.append(Problem("error", f"{at}.{key}", f"{shown} must be a path inside the repository: relative, "
                                    f"at most {PATH_MAX} characters, with no `..`, backslash, leading `-` or control "
                                    "character"))
            paths[key] = ""
    return BacklogItem(
        ident, deps, title=title, lane=lane, status=status, spec=paths["spec"], plan=paths["plan"], issue=issue, notes=notes
    )


def parse(text: str) -> tuple[list[BacklogItem], list[Problem]]:
    """The items of a backlog file's text and every problem found. Never raises."""
    found, problems = parse_indexed(text)
    return [item for _, item in found], problems


def parse_indexed(text: str) -> tuple[list[tuple[int, BacklogItem]], list[Problem]]:
    """`parse`, each kept item with its position in the file's `items` list (the `n` of `items[n]` in a problem's
    `where`), so a caller can name an item the parser dropped neighbours of."""

    def broken(where: str, message: str) -> tuple[list[tuple[int, BacklogItem]], list[Problem]]:
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
    items: list[tuple[int, BacklogItem]] = []
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
        items.append((n, item))
    return items, problems


def read(path: Path) -> tuple[list[BacklogItem], list[Problem]]:
    """`parse` of the file at `path`, read without following a symlink. Never raises."""
    found, problems = read_indexed(path)
    return [item for _, item in found], problems


def read_indexed(path: Path) -> tuple[list[tuple[int, BacklogItem]], list[Problem]]:
    """`parse_indexed` of the file at `path`, read without following a symlink. Never raises."""
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
    return parse_indexed(text)


# --- rendering and in-place edits -------------------------------------------------------------------------------

HEADER = (
    "# backlog.yml: every backlog item, highest priority first. `agentflow validate` checks it.\n"
    "# status: backlog (an idea) | ready (may start) | done | dropped. A run's phase is never written here: the Board\n"
    "# shows it from the run's claim (refs/agentflow/claims/<id>).\n"
)
_PLAIN = re.compile(r"[A-Za-z0-9_./][A-Za-z0-9_ ./,()'+=@-]*")
_UNREADABLE = re.compile("[^\x09\x0a\x0d\x20-\x7e\xa0-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
_LINE = re.compile(r"[^\r\n]*(?:\r\n|\n|\r)|[^\r\n]+")
_KEY_LINE = re.compile(r"( *)([A-Za-z_][A-Za-z0-9_]*)[ \t]*:(?:[ \t]+(.*))?\r?\n?$")
# group 3 is left untrimmed on purpose: a lazy group before trailing blanks backtracks quadratically on a long run
_DEFAULTS = {"lane": "", "spec": "", "plan": "", "depends_on": [], "issue": None, "notes": ""}
_FIELD = {"depends_on": "deps"}


class EditRefused(ValueError):
    """An edit that could not be made safely. The file is untouched."""


def _reads_back(text: str, as_value: str, expect: object) -> bool:
    try:
        return yaml.safe_load(text) == expect
    except (yaml.YAMLError, RecursionError, ValueError):
        return False


def _scalar(value: str, flow: bool = False) -> str:
    """`value` as YAML that reads back as exactly this string: plain when that is certain, else a JSON string."""
    if _PLAIN.fullmatch(value) and value == value.strip():
        probe = f"[{value}]" if flow else f"k: {value}"
        if _reads_back(probe, value, [value] if flow else {"k": value}):
            return value
    quoted = json.dumps(value, ensure_ascii=False)
    return _UNREADABLE.sub(lambda m: f"\\u{ord(m.group()):04x}", quoted)


def _entry(key: str, value: object) -> str:
    """The `key: value` text of one field. `EditRefused` for a value of the wrong type."""
    if key == "depends_on":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise EditRefused("depends_on must be a list of item ids")
        return "depends_on: [" + ", ".join(_scalar(v, flow=True) for v in value) + "]"
    if key == "issue":
        if isinstance(value, bool) or not isinstance(value, int):
            raise EditRefused("issue must be a positive integer")
        return f"issue: {value}"
    if not isinstance(value, str):
        raise EditRefused(f"{key} must be a string")
    return f"{key}: {_scalar(value)}"


def _empty(key: str, value: object) -> bool:
    return key in _DEFAULTS and (value is None or value == _DEFAULTS[key])


def _block(item: BacklogItem, indent: int = 2) -> list[str]:
    """The lines (without endings) of one item: `status` always, and the other keys when they hold something."""
    values = {
        "id": item.id, "title": item.title, "lane": item.lane, "status": item.status, "spec": item.spec,
        "plan": item.plan, "depends_on": item.deps, "issue": item.issue, "notes": item.notes,
    }  # fmt: skip
    pad = " " * (indent + 2)
    lines = [" " * indent + "- " + _entry("id", item.id)]
    for key in KEYS[1:]:
        if not _empty(key, values[key]):
            lines.append(pad + _entry(key, values[key]))
    return lines


def render(items: list[BacklogItem]) -> str:
    """The file's text for `items`: the header, `version: 1`, and each item with its keys in `KEYS` order."""
    out = [HEADER, "version: 1\n"]
    if not items:
        out.append("items: []\n")
    else:
        out.append("items:\n")
        out.extend(line + "\n" for item in items for line in _block(item))
    return "".join(out)


def _load(path: Path) -> str:
    try:
        data = safeio.read_bytes(path, FILE_MAX + 1)
    except safeio.SymlinkRefused:
        raise EditRefused(f"{path.name} is a symlink") from None
    except (OSError, ValueError) as exc:
        raise EditRefused(f"{path.name} cannot be read: {exc}") from None
    if data is None:
        raise EditRefused(f"{path.name} is missing: agentflow backlog import writes it")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise EditRefused(f"{path.name} is not valid UTF-8") from None


def _store(path: Path, text: str) -> None:
    if len(text.encode("utf-8")) > FILE_MAX:
        raise EditRefused(f"the edit would make {path.name} larger than {FILE_MAX} bytes")
    try:
        safeio.write_text(path, text)
    except (OSError, ValueError) as exc:
        raise EditRefused(f"{path.name} cannot be written: {exc}") from None


def _ending(line: str) -> str:
    return line[len(line.rstrip("\r\n")) :]


def _skippable(line: str) -> bool:
    """A blank line or a comment: neither belongs to a block's structure."""
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _new_problems(before: list[Problem], after: list[Problem]) -> list[Problem]:
    return [p for p in after if p not in before]


def _find_block(lines: list[str], item_id: str) -> tuple[int, int, int]:
    """(index of the `- id:` line, index after the item's last line, column of its keys). `EditRefused` otherwise."""
    flow = re.compile(r"\s*-\s*\{.*\bid\s*:\s*[\"']?" + re.escape(item_id) + r"[\"']?\s*[,}]")
    start = re.compile(r"( *)-( +)id:[ \t]*[\"']?" + re.escape(item_id) + r"[\"']?[ \t]*(?:#.*)?\r?\n?$")
    for n, line in enumerate(lines):
        if flow.match(line):
            raise EditRefused(f"item {item_id} is written in flow style: edit it by hand")
        found = start.match(line)
        if not found:
            continue
        dash, key_col = len(found.group(1)), len(found.group(1)) + 1 + len(found.group(2))
        end = n + 1
        for j in range(n + 1, len(lines)):
            if _skippable(lines[j]):
                continue
            if _indent(lines[j]) <= dash:
                break
            end = j + 1
        return n, end, key_col
    raise EditRefused(f"item {item_id} is not written as a block with `- id: {item_id}` on its first line")


def _key_lines(lines: list[str], first: int, end: int, col: int, item_id: str) -> dict[str, tuple[int, int]]:
    """Each key line of the block in `lines[first:end]` -> (line index, index after its value). Refuses a block that is
    not one key per line at column `col`."""
    keys: dict[str, tuple[int, int]] = {}
    order: list[tuple[str, int]] = []
    for j in range(first, end):
        line = lines[j]
        if _skippable(line):
            continue
        if _indent(line) == col:
            found = _KEY_LINE.match(line)
            if found is None:
                raise EditRefused(f"item {item_id} is not written one key per line")
            order.append((found.group(2), j))
        elif _indent(line) < col or not order:
            raise EditRefused(f"item {item_id} is not written one key per line")
    for n, (key, j) in enumerate(order):
        after = order[n + 1][1] if n + 1 < len(order) else end
        keys.setdefault(key, (j, after))
    return keys


def set_fields(path: Path, item_id: str, fields: dict[str, object]) -> None:
    """Set `fields` of the item `item_id` in the file at `path`, changing only those lines (`set_fields_text`).
    `EditRefused`, with the file untouched, when it cannot."""
    _store(path, set_fields_text(_load(path), item_id, fields, name=path.name))


def set_fields_text(text: str, item_id: str, fields: dict[str, object], *, name: str = FILE) -> str:
    """`text` with `fields` of the item `item_id` set, changing only those lines. A key's line is replaced, or inserted
    after the `- id:` line; an empty value (`""`, `[]`, `None`) removes the key's line. The new text is parsed first: the
    item must hold the new values and nothing else may change. `EditRefused` when it cannot; `name` is the file's name
    in its message."""
    for key in fields:
        if key == "id":
            raise EditRefused("an item's id is never renamed")
        if key not in KEYS:
            raise EditRefused(f"{key!r} is not a backlog key")
    # an empty value removes the key's line (`None` here); `status` and `title` are required, so they are always written
    entries = {k: None if _empty(k, v) else _entry(k, v) for k, v in fields.items()}
    items, problems = parse(text)
    index = next((n for n, item in enumerate(items) if item.id == item_id), None)
    if index is None:
        raise EditRefused(f"item {item_id} is not in {name}, or has an error")
    lines = _LINE.findall(text)
    first, end, col = _find_block(lines, item_id)
    keys = _key_lines(lines, first + 1, end, col, item_id)
    nl = _ending(lines[first]) or ("\r\n" if "\r\n" in text else "\n")
    out = list(lines)
    added: list[str] = []
    for key in KEYS[1:]:
        if key not in entries:
            continue
        entry = entries[key]
        if key in keys:
            at, after = keys[key]
            found = _KEY_LINE.match(lines[at])
            value = (found.group(3) if found else "") or ""
            if value[:1] in ("|", ">") or any(not _skippable(lines[j]) for j in range(at + 1, after)):
                raise EditRefused(f"{key} of item {item_id} spans several lines: edit it by hand")
            out[at] = "" if entry is None else " " * col + entry + _ending(lines[at])
        elif entry is not None:
            added.append(" " * col + entry + nl)
    if added:
        if not _ending(out[first]):
            out[first] += nl
            added[-1] = added[-1][: -len(nl)]
        out[first + 1 : first + 1] = added
    new = "".join(out)
    expected = replace(
        items[index], **{_FIELD.get(k, k): (_DEFAULTS[k] if entries[k] is None else v) for k, v in fields.items()}
    )
    after_items, after_problems = parse(new)
    if (
        len(after_items) != len(items)
        or after_items[index] != expected
        or any(a != b for n, (a, b) in enumerate(zip(after_items, items, strict=True)) if n != index)
        or _new_problems(problems, after_problems)
    ):
        raise EditRefused(f"the new values for item {item_id} are not valid: {_why(problems, after_problems, fields)}")
    return new


def _why(before: list[Problem], after: list[Problem], fields: dict[str, object]) -> str:
    fresh = _new_problems(before, after)
    return "; ".join(f"{p.where} {p.message}" for p in fresh) or ", ".join(f"{k}={v!r}" for k, v in fields.items())


def append_item(path: Path, item: BacklogItem) -> None:
    """Add `item` after the last item of the file at `path` (`append_item_text`). `EditRefused`, with the file
    untouched, for a missing file or anything `append_item_text` refuses."""
    _store(path, append_item_text(_load(path), item, name=path.name))


def append_item_text(text: str, item: BacklogItem, *, name: str = FILE) -> str:
    """`text` with `item` added after its last item, every other line as it is. `EditRefused` for a duplicate id, a list
    written in flow style, or an item that does not parse; `name` is the file's name in its message."""
    items, problems = parse(text)
    if any(other.id == item.id for other in items):
        raise EditRefused(f"item {item.id} is already in {name}")
    lines = _LINE.findall(text)
    head = next((n for n, line in enumerate(lines) if re.match(r"items:", line)), None)
    if head is None:
        raise EditRefused(f"{name} has no `items:` list")
    rest = lines[head][len("items:") :].split("#", 1)[0].strip()  # no regex: the line may hold a long run of blanks
    nl = _ending(lines[head]) or ("\r\n" if "\r\n" in text else "\n")
    out = list(lines)
    if rest == "":
        last = head
        indent = 2
        for j in range(head + 1, len(lines)):
            if _skippable(lines[j]):
                continue
            if _indent(lines[j]) == 0 and not lines[j].startswith("- "):
                break
            if last == head:
                found = re.match(r"( *)- ", lines[j])
                if found is None:
                    raise EditRefused(f"the items list of {name} is not written as a block")
                indent = len(found.group(1))
            last = j
        at = last + 1
        if last != head and not _ending(out[last]):
            out[last] += nl
    elif re.fullmatch(r"\[\s*\]", rest):
        out[head] = "items:" + nl
        at = head + 1
    else:
        raise EditRefused(f"the items list of {name} is written in flow style: edit it by hand")
    block = [line + nl for line in _block(item, indent if rest == "" else 2)]
    out[at:at] = block
    new = "".join(out)
    after_items, after_problems = parse(new)
    if after_items != [*items, item] or _new_problems(problems, after_problems):
        raise EditRefused(f"item {item.id} is not valid: {_why(problems, after_problems, {})}")
    return new
