"""Changing `config.yaml` from the dashboard's Settings page, keeping the file's comments and layout.

Two ways in, both through `propose` (check, nothing written) and `save` (check, then write):
- the form sends `changes`: `[{"key": ["defaults", "step_timeout_min"], "value": 60}, ...]`. A key must be a
  field of `form_fields` and its value must have that field's kind. Each change becomes an edit of the file's
  text at the value's place (found from PyYAML's node marks), so comments, anchors and the flow or block style
  around it stay as they are. A mapping is changed key by key; a key left out of a new mapping is removed.
- the YAML tab sends the whole `text`.

Either way the new text must parse, load (`load_config`) and validate (`validate` + `settings_coverage`, as
`agentflow validate` does) with no errors, and leave the protected settings (`PROTECTED`) exactly as they were.
Those decide which shell commands the runner runs and what fences its agents in (gates, guardrails, conditions,
an agent's command line, the environment of every session), so a dashboard token never becomes a way to change
them: they are changed in the file, in git, where a pull request shows it. A form edit is also checked to parse
back to exactly the intended data, so a text edit that went wrong is refused instead of written.

Every proposal carries the SHA-256 of the text it was made from (`base_sha256`). A save whose base is not the
file's current content is refused (`Conflict`), so two tabs, or a tab and an editor, never overwrite each other.
The file is replaced atomically (a temporary file in the same folder, then `os.replace`), never through a symlink.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import math
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from agentflow.config import KNOWN_EFFORT_LEVELS, ConfigError, Issue, load_config, validate

__all__ = ["Conflict", "EditError", "Proposal", "Refused", "form_fields", "propose", "read", "save"]

MAX_TEXT = 512 * 1024
MERGE_TAG = "tag:yaml.org,2002:merge"
AGENT_FREE_KEYS = ("enabled", "models", "max_turns", "prices")  # what a page may change in an `agents.<name>` block
PHASE_FREE_KEYS = ("needs_approval", "resolve", "model", "agent")  # the rest of a phase is the workflow itself
_PROTECTED_KEYS = (  # top-level blocks and dotted keys: commands, fences, and paths or refs the runner reads or writes
    "version", "engine", "gates", "gate_sets", "guardrails", "conditions", "overlays", "project", "phase_approval",
    "record_step", "resolve_step", "defaults.env", "context.autocompact_env", "context.progress_file", "hitl.notify",
    "dashboard.host",
)
PROTECTED = (
    *_PROTECTED_KEYS,
    "agents.<name> (all but enabled, models, max_turns, prices)",
    "phases.<name> (all but needs_approval, resolve, model, agent)",
)
_LOCK = threading.Lock()


class EditError(Exception):
    """A change that cannot be made as asked."""


class Conflict(EditError):
    """The file changed since the page read it."""


class Refused(EditError):
    """A change to a setting the page may not change."""


@dataclass
class Proposal:
    base_sha256: str
    text: str
    diff: str
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.level == "error"]

    @property
    def changed(self) -> bool:
        return bool(self.diff)

    def as_dict(self) -> dict:
        return {"base_sha256": self.base_sha256, "sha256": sha256(self.text), "diff": self.diff,
                "changed": self.changed, "ok": not self.errors,
                "issues": [{"level": i.level, "path": i.path, "message": i.message} for i in self.issues]}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read(path: Path) -> tuple[str, str]:
    """`(text, sha256)` of the config file, refusing a symlink."""
    path = Path(path)
    if path.is_symlink():
        raise EditError(f"{path} is a symlink; the dashboard edits only a regular file")
    text = path.read_text(encoding="utf-8")
    return text, sha256(text)


# --------------------------------------------------------------------------- the form's fields

# (section, key, label, kind, extra). A key whose parent block is missing from the file is left out of the form.
_INT = {"kind": "int", "min": 0}
_POS = {"kind": "int", "min": 1}
_NUM = {"kind": "number", "min": 0}
_PCT = {"kind": "int", "min": 1, "max": 100}

_SECTIONS: list[tuple[str, str, list[tuple[tuple[str, ...], str, dict]]]] = [
    ("Run defaults", "A running run picks these up when its next phase starts; the budget is set when a run starts.", [
        (("defaults", "agent"), "Default agent", {"kind": "enum", "options": "@agents"}),
        (("defaults", "step_timeout_min"), "Step timeout (min)", _POS),
        (("defaults", "gate_timeout_min"), "Gate timeout (min)", {"kind": "number", "min": 0, "exclusive_min": True}),
        (("defaults", "fix_attempts"), "Fix attempts per gate failure", _INT),
        (("defaults", "run_budget_usd"), "Run budget (USD, new runs)", {"kind": "number", "min": 0, "exclusive_min": True}),
        (("defaults", "task_budget_usd"), "Budget per plan task (USD, 0 = off)", {"kind": "number", "min": 0}),
    ]),
    ("Recovery", "How long the runner keeps trying on its own after an outage or a failed call.", [
        (("recovery", "give_up_h"), "Give up after (h)", _NUM),
        (("recovery", "failed_attempts"), "Failed attempts", _POS),
    ]),
    ("Effort", "The reasoning effort levels an agent may pick for a step.", [
        (("effort", "default"), "Starting level", {"kind": "enum", "options": list(KNOWN_EFFORT_LEVELS)}),
        (("effort", "allowed"), "Allowed levels", {"kind": "list", "options": list(KNOWN_EFFORT_LEVELS)}),
    ]),
    ("Context", "When sessions compact or roll over.", [
        (("context", "autocompact_pct"), "Auto-compact at (%)", _PCT),
        (("context", "rollover_pct"), "Roll over at (%)", _PCT),
        (("context", "max_rollovers"), "Max rollovers", _INT),
        (("context", "default_window_tokens"), "Default window (tokens)", _POS),
        (("context", "gate_output_lines"), "Gate output lines", _POS),
    ]),
    ("Autonomy", "What the runner settles by itself before a human hears of it.", [
        (("autonomy", "non_blocking_severities"), "Non-blocking severities", {"kind": "list"}),
        (("autonomy", "finding_rounds"), "Finding rounds", _INT),
        (("autonomy", "answer_recommended"), "Take recommended answers", {"kind": "bool"}),
        (("autonomy", "revert_forbidden"), "Forbidden-path reverts per step", _INT),
        (("autonomy", "resolve_kinds"), "Resolver sessions per stop kind", {"kind": "map", "value_kind": "int", "min": 1}),
    ]),
    ("CI", "The `ci` step in ship.", [
        (("ci", "poll_s"), "Poll every (s)", _POS),
        (("ci", "appear_min"), "Checks must appear within (min)", _NUM),
        (("ci", "timeout_min"), "Pending timeout (min)", _NUM),
        (("ci", "fix_rounds"), "Fix rounds", _INT),
        (("ci", "rerun_rounds"), "Rerun rounds", _INT),
    ]),
    ("Human in the loop", "Reminders and parking.", [
        (("hitl", "remind_after_h"), "Remind after (h)", _NUM),
        (("hitl", "park_independent"), "Park independent questions", {"kind": "bool"}),
    ]),
    ("Dashboard", "The page reads these at once; a new port needs a dashboard restart.", [
        (("dashboard", "port"), "Port (restart)", {"kind": "int", "min": 0, "max": 65535}),
        (("dashboard", "show_done_days"), "Show done cards for (days, 0 = always)", _INT),
        (("dashboard", "agents", "show_exited_min"), "Show exited agents for (min)", _INT),
        (("dashboard", "agents", "idle_warn_min"), "Warn when idle for (min)", _INT),
        (("dashboard", "backlog", "sync_base"), "Fast-forward the base branch", {"kind": "bool"}),
        (("dashboard", "backlog", "module_lane"), "Lane for map modules", {"kind": "enum", "options": "@lanes"}),
        (("dashboard", "backlog", "lane_labels"), "Lane labels", {"kind": "map", "value_kind": "text"}),
    ]),
]


def _get(data: Any, key: tuple[str, ...]) -> tuple[bool, Any]:
    for part in key:
        if not isinstance(data, dict) or part not in data:
            return False, None
        data = data[part]
    return True, data


def _catalog(raw: dict) -> list[dict]:
    """The form's sections, each with its fields' definitions (no values yet)."""
    sections = []
    for title, note, rows in _SECTIONS:
        fields = [{"key": list(key), "label": label, **extra} for key, label, extra in rows]
        sections.append({"title": title, "note": note, "fields": fields})
    for name, spec in (raw.get("agents") or {}).items():
        if not isinstance(spec, dict):
            continue
        sections.append({"title": f"Agent: {name}", "note": "Models by role and the turn limit for every session "
                         "on this agent. Its command line is protected.", "fields": [
            {"key": ["agents", name, "enabled"], "label": "Enabled", "kind": "bool"},
            {"key": ["agents", name, "models"], "label": "Model by role", "kind": "map", "value_kind": "text"},
            {"key": ["agents", name, "max_turns"], "label": "Max turns", "kind": "int", "min": 1},
        ]})
    phase_fields = []
    for name, phase in (raw.get("phases") or {}).items():
        if not isinstance(phase, dict):
            continue
        phase_fields.append({"key": ["phases", name, "needs_approval"], "label": f"{name}: a human approves",
                             "kind": "bool"})
        phase_fields.append({"key": ["phases", name, "resolve"], "label": f"{name}: resolver first", "kind": "bool"})
        model = phase.get("model")
        if isinstance(model, dict):
            phase_fields.append({"key": ["phases", name, "model"], "label": f"{name}: model by role", "kind": "map",
                                 "value_kind": "text"})
        else:
            phase_fields.append({"key": ["phases", name, "model"], "label": f"{name}: model", "kind": "text",
                                 "optional": True})
    if phase_fields:
        sections.append({"title": "Phases", "note": "A phase's steps are the workflow: change them in the file.",
                         "fields": phase_fields})
    return sections


def _options(field_def: dict, raw: dict) -> list[str] | None:
    options = field_def.get("options")
    if options == "@agents":
        return [str(k) for k in (raw.get("agents") or {})]
    if options == "@lanes":
        return [str(k) for k in (raw.get("lanes") or [])]
    return options


def form_fields(path: Path) -> dict:
    """What the Settings page shows: the file's text and hash, the form (each field with its value and the
    file's comment on that line), and the protected settings."""
    text, digest = read(path)
    raw = yaml.safe_load(text) or {}
    comments = _comments(text)
    sections = []
    for section in _catalog(raw):
        fields = []
        for fdef in section["fields"]:
            key = tuple(fdef["key"])
            parent_ok, _ = _get(raw, key[:-1])
            if not parent_ok:
                continue
            present, value = _get(raw, key)
            item = {**fdef, "value": value, "present": present, "help": comments.get(key, "")}
            options = _options(fdef, raw)
            if options is not None:
                item["options"] = options
            if isinstance(value, str) and value.startswith("@"):
                item["readonly"] = "a reference to another key; change it in the YAML tab"
            fields.append(item)
        if fields:
            sections.append({**section, "fields": fields})
    return {"path": str(path), "sha256": digest, "text": text, "sections": sections, "protected": list(PROTECTED)}


def _field_index(raw: dict) -> dict[tuple[str, ...], dict]:
    index = {}
    for section in _catalog(raw):
        for fdef in section["fields"]:
            fdef = {**fdef}
            options = _options(fdef, raw)
            if options is not None:
                fdef["options"] = options
            index[tuple(fdef["key"])] = fdef
    return index


def _number_ok(value: Any, fdef: dict, whole: bool) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if whole and not isinstance(value, int):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    low, high = fdef.get("min"), fdef.get("max")
    if low is not None and (value <= low if fdef.get("exclusive_min") else value < low):
        return False
    return high is None or value <= high


def _check_value(fdef: dict, value: Any) -> str | None:
    """Why `value` does not fit the field, or None."""
    kind = fdef["kind"]
    label = fdef["label"]
    if value is None and fdef.get("optional"):
        return None
    if kind == "bool":
        return None if isinstance(value, bool) else f"{label} must be true or false"
    if kind in ("int", "number"):
        if _number_ok(value, fdef, kind == "int"):
            return None
        bounds = [f">{'' if fdef.get('exclusive_min') else '='} {fdef['min']}"] if "min" in fdef else []
        bounds += [f"<= {fdef['max']}"] if "max" in fdef else []
        what = "a whole number" if kind == "int" else "a number"
        return f"{label} must be {what}" + (f" ({', '.join(bounds)})" if bounds else "")
    if kind in ("text", "enum"):
        if not isinstance(value, str) or not value.strip() or "\n" in value or len(value) > 500:
            return f"{label} must be one line of text"
        if kind == "enum" and value not in fdef["options"]:
            return f"{label} must be one of {', '.join(fdef['options'])}"
        return None
    if kind == "list":
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() and "\n" not in v for v in value):
            return f"{label} must be a list of one-line texts"
        if fdef.get("options") and any(v not in fdef["options"] for v in value):
            return f"{label} may only hold {', '.join(fdef['options'])}"
        return None
    if kind == "map":
        if not isinstance(value, dict) or not all(isinstance(k, str) and k.strip() and "\n" not in k for k in value):
            return f"{label} must map names to values"
        inner = {"kind": fdef.get("value_kind", "text"), "label": f"each value of {label}", "min": fdef.get("min")}
        if inner["min"] is None:
            inner.pop("min")
        for v in value.values():
            problem = _check_value(inner, v)
            if problem:
                return problem
        return None
    return f"{label} cannot be changed here"


# --------------------------------------------------------------------------- proposals


def propose(path: Path, base_sha256: str, *, changes: list | None = None, text: str | None = None,
            repo: Path | None = None) -> Proposal:
    """The text a save would write and what `validate` says about it; raises `Conflict`, `Refused`, `EditError`."""
    current, digest = read(path)
    if base_sha256 != digest:
        raise Conflict("config.yaml changed since the page loaded it; reload the Settings page and redo the change")
    if (changes is None) == (text is None):
        raise EditError("send either changes (from the form) or text (from the YAML tab)")
    if text is not None:
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_TEXT:
            raise EditError(f"text must be at most {MAX_TEXT} bytes")
        new_text = text.replace("\r\n", "\n")
    else:
        new_text = _apply_form(current, changes)
    issues = _check(path, new_text, repo or path.parent)
    diff = "".join(difflib.unified_diff(current.splitlines(keepends=True), new_text.splitlines(keepends=True),
                                        "config.yaml", "config.yaml (new)"))
    return Proposal(digest, new_text, diff, issues)


def save(path: Path, proposal: Proposal) -> str:
    """Write a checked proposal; the new SHA-256. Raises `Conflict` if the file changed meanwhile."""
    if proposal.errors:
        raise EditError("the change does not validate: " + "; ".join(f"{i.path}: {i.message}" for i in proposal.errors))
    path = Path(path)
    with _LOCK:
        _, digest = read(path)
        if digest != proposal.base_sha256:
            raise Conflict("config.yaml changed since the page loaded it; reload the Settings page and redo the change")
        mode = path.stat().st_mode & 0o777
        fd, temp = tempfile.mkstemp(prefix=".config-", suffix=".yaml.tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(proposal.text)
            os.chmod(temp, mode)
            os.replace(temp, path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise
    return sha256(proposal.text)


def _check(path: Path, new_text: str, repo: Path) -> list[Issue]:
    """Load and validate `new_text` beside `path` (so `prompts/` resolves), and hold the protected settings."""
    try:
        parsed = yaml.safe_load(new_text)
    except yaml.YAMLError as exc:
        return [Issue("error", "", f"not valid YAML: {exc}")]
    if not isinstance(parsed, dict):
        return [Issue("error", "", "the file must hold a mapping at the top level")]
    fd, temp = tempfile.mkstemp(prefix=".config-check-", suffix=".yaml", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(new_text)
        try:
            new_cfg = load_config(Path(temp))
        except ConfigError as exc:
            _hold_unloadable(path, parsed)  # a pack format or engine change does not load: it is still refused
            return [Issue("error", exc.path, exc.message)]
        from agentflow.guards import settings_coverage

        issues = validate(new_cfg, repo) + settings_coverage(new_cfg, repo)
    finally:
        Path(temp).unlink(missing_ok=True)
    try:
        old_raw = load_config(path).raw
    except ConfigError:  # the file on disk is broken already: there is nothing to hold
        old_raw = None
    if old_raw is not None:
        changed = _protected_changes(old_raw, new_cfg.raw)
        if changed:
            raise Refused("these settings are protected and can only be changed in the file, in git: "
                          + ", ".join(changed))
    return issues


def _hold_unloadable(path: Path, parsed: dict) -> None:
    """Refuse a text that changes a protected setting even when it no longer loads, comparing it as written with
    the file as written (a loaded config's `@` references are resolved, the new text's are not)."""
    try:
        load_config(path)
        old = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (ConfigError, OSError, yaml.YAMLError):  # the file on disk is broken already: there is nothing to hold
        return
    changed = _protected_changes(old, parsed)
    if changed:
        raise Refused("these settings are protected and can only be changed in the file, in git: " + ", ".join(changed))


def _protected_view(raw: dict) -> dict[str, Any]:
    view: dict[str, Any] = {}
    for dotted in _PROTECTED_KEYS:
        view[dotted] = _get(raw, tuple(dotted.split(".")))
    agents: dict = raw["agents"] if isinstance(raw.get("agents"), dict) else {}
    view["agents (names)"] = sorted(agents)
    for name, spec in agents.items():
        spec = spec if isinstance(spec, dict) else {"": spec}
        view[f"agents.{name}"] = {k: v for k, v in spec.items() if k not in AGENT_FREE_KEYS}
    phases: dict = raw["phases"] if isinstance(raw.get("phases"), dict) else {}
    view["phases (names and order)"] = list(phases)
    for name, phase in phases.items():
        phase = phase if isinstance(phase, dict) else {"": phase}
        view[f"phases.{name}"] = {k: v for k, v in phase.items() if k not in PHASE_FREE_KEYS}
    return view


def _protected_changes(old: dict, new: dict) -> list[str]:
    before, after = _protected_view(old), _protected_view(new)
    return [key for key in dict.fromkeys([*before, *after]) if before.get(key) != after.get(key)]


# --------------------------------------------------------------------------- form changes as text edits


def _apply_form(text: str, changes: Any) -> str:
    if not isinstance(changes, list) or not changes:
        raise EditError("changes must be a non-empty list")
    raw = yaml.safe_load(text) or {}
    index = _field_index(raw)
    expected = copy.deepcopy(raw)
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"key", "value"}:
            raise EditError("each change is an object with exactly key and value")
        key = change["key"]
        if not isinstance(key, list) or not all(isinstance(k, str) for k in key) or tuple(key) not in index:
            raise Refused(f"{key!r} is not a setting this page can change; use the YAML tab")
        fdef = index[tuple(key)]
        if fdef.get("readonly"):
            raise Refused(f"{'.'.join(key)} cannot be changed here")
        present, old_value = _get(raw, tuple(key))
        if present and isinstance(old_value, str) and old_value.startswith("@"):
            raise Refused(f"{'.'.join(key)} is a reference to another key; change it in the YAML tab")
        problem = _check_value(fdef, change["value"])
        if problem:
            raise EditError(problem)
        value = change["value"]
        if value is None:
            text = _edit(text, tuple(key), _DELETE)
            _drop(expected, tuple(key))
        else:
            text = _set_deep(text, tuple(key), value)
            _put(expected, tuple(key), value)
    if yaml.safe_load(text) != expected:
        raise EditError("the change could not be made while keeping the file's layout; make it in the YAML tab")
    return text


def _put(data: dict, key: tuple[str, ...], value: Any) -> None:
    for part in key[:-1]:
        data = data.setdefault(part, {})
    data[key[-1]] = copy.deepcopy(value)


def _drop(data: dict, key: tuple[str, ...]) -> None:
    present, parent = _get(data, key[:-1])
    if present and isinstance(parent, dict):
        parent.pop(key[-1], None)


_DELETE = object()


def _set_deep(text: str, key: tuple[str, ...], value: Any) -> str:
    """Set `key` to `value`; a mapping value is set key by key, so the block's own layout stays."""
    present, old = _get(yaml.safe_load(text) or {}, key)
    if present and isinstance(old, dict) and isinstance(value, dict):
        for child in old:
            if child not in value:
                text = _edit(text, (*key, str(child)), _DELETE)
        for child, inner in value.items():
            if child in old and old[child] == inner:
                continue
            text = _set_deep(text, (*key, child), inner)
        return text
    if present and old == value and type(old) is type(value):
        return text
    return _edit(text, key, value)


def _render(value: Any) -> str:
    """`value` as YAML that is safe anywhere a flow-context value may stand (one line)."""
    out = yaml.safe_dump([value], default_flow_style=True, width=1 << 30, allow_unicode=True, sort_keys=False).strip()
    if not (out.startswith("[") and out.endswith("]")) or "\n" in out:
        raise EditError(f"cannot write {value!r} on one line")
    return out[1:-1].strip()


def _shared_nodes(root: Node) -> set[int]:
    """Ids of nodes reached more than once (an anchor with aliases), and of everything inside them."""
    counts: dict[int, int] = {}

    def count(node: Node) -> None:
        counts[id(node)] = counts.get(id(node), 0) + 1
        if counts[id(node)] == 1:
            for child in _children(node):
                count(child)

    count(root)
    return {id(inner) for node in _all_nodes(root) if counts[id(node)] > 1 for inner in _all_nodes(node)}


def _children(node: Node) -> list[Node]:
    if isinstance(node, MappingNode):
        return [n for pair in node.value for n in pair]
    if isinstance(node, SequenceNode):
        return list(node.value)
    return []


def _all_nodes(root: Node) -> list[Node]:
    out, stack, seen = [], [root], set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        out.append(node)
        stack.extend(_children(node))
    return out


def _entry(mapping: MappingNode, key: str) -> tuple[int, Node, Node] | None:
    for i, (k, v) in enumerate(mapping.value):
        if isinstance(k, ScalarNode) and k.tag != MERGE_TAG and k.value == key:
            return i, k, v
    return None


def _last_end(node: Node) -> int:
    """The text index just after the last character that belongs to `node`'s value."""
    if isinstance(node, (MappingNode, SequenceNode)) and not node.flow_style and node.value:
        last = node.value[-1]
        return _last_end(last[1] if isinstance(node, MappingNode) else last)
    return node.end_mark.index


def _line_start(text: str, index: int) -> int:
    return text.rfind("\n", 0, index) + 1


def _line_end(text: str, index: int) -> int:
    end = text.find("\n", index)
    return len(text) if end == -1 else end


def _edit(text: str, key: tuple[str, ...], value: Any) -> str:
    """One text edit: replace, add (with any missing parents) or delete (`_DELETE`) the value at `key`."""
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    if not isinstance(root, MappingNode):
        raise EditError("the file must hold a mapping at the top level")
    shared = _shared_nodes(root)
    node: MappingNode = root
    for depth, part in enumerate(key):
        if id(node) in shared:
            raise EditError(f"{'.'.join(key)} sits in an anchored block used elsewhere; change it in the YAML tab")
        found = _entry(node, part)
        last = depth == len(key) - 1
        if found is None:
            if value is _DELETE:
                return text
            rest = key[depth + 1:]
            nested: Any = value
            for inner in reversed(rest):
                nested = {inner: nested}
            return _add(text, node, part, nested)
        index, _, vnode = found
        if last:
            if id(vnode) in shared:
                raise EditError(f"{'.'.join(key)} is anchored and used elsewhere; change it in the YAML tab")
            if value is _DELETE:
                return _delete(text, node, index)
            return _replace(text, vnode, value)
        if not isinstance(vnode, MappingNode):
            if value is _DELETE:
                return text
            raise EditError(f"{'.'.join(key[:depth + 1])} is not a mapping")
        node = vnode
    return text


def _replace(text: str, node: Node, value: Any) -> str:
    start, end = node.start_mark.index, _last_end(node)
    return text[:start] + _render(value) + text[end:]


def _add(text: str, mapping: MappingNode, key: str, value: Any) -> str:
    entry = f"{_render(key)}: {_render(value)}"
    if mapping.flow_style:
        close = mapping.end_mark.index - 1
        if text[close] != "}":
            raise EditError("cannot find the end of a flow mapping")
        if mapping.value:
            at = _last_end(mapping.value[-1][1])
            return text[:at] + ", " + entry + text[at:]
        return text[:close] + entry + text[close:]
    if not mapping.value:
        raise EditError("cannot add to an empty block")
    column = mapping.value[0][0].start_mark.column
    at = _line_end(text, _last_end(mapping.value[-1][1]))
    return text[:at] + "\n" + " " * column + entry + text[at:]


def _delete(text: str, mapping: MappingNode, index: int) -> str:
    knode, vnode = mapping.value[index]
    if mapping.flow_style:
        if index + 1 < len(mapping.value):
            return text[:knode.start_mark.index] + text[mapping.value[index + 1][0].start_mark.index:]
        if index > 0:
            return text[:_last_end(mapping.value[index - 1][1])] + text[_last_end(vnode):]
        return text[:knode.start_mark.index] + text[_last_end(vnode):]
    if len(mapping.value) == 1:
        return text[:knode.start_mark.index] + "{}" + text[_last_end(vnode):]
    start = _line_start(text, knode.start_mark.index)
    if text[start:knode.start_mark.index].strip():
        raise EditError("cannot remove a key that shares its line with another")
    end = _line_end(text, _last_end(vnode))
    return text[:start] + text[min(end + 1, len(text)):]


def _comments(text: str) -> dict[tuple[str, ...], str]:
    """The comment at the end of each key's line (`key: value   # this`), by key path."""
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return {}
    out: dict[tuple[str, ...], str] = {}

    def walk(node: Node, path: tuple[str, ...]) -> None:
        if not isinstance(node, MappingNode):
            return
        for k, v in node.value:
            if not isinstance(k, ScalarNode) or k.tag == MERGE_TAG:
                continue
            here = (*path, k.value)
            line_end = _line_end(text, k.end_mark.index)
            after = v.end_mark.index if v.start_mark.line == k.start_mark.line and v.end_mark.line == k.start_mark.line \
                else k.end_mark.index
            tail = text[min(after, line_end):line_end]
            hash_at = tail.find("#")
            if hash_at != -1 and (hash_at == 0 or tail[hash_at - 1] in " \t"):
                out[here] = tail[hash_at + 1:].strip()
            walk(v, here)

    walk(root, ())
    return out
