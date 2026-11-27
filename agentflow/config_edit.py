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

The user's own file (`layers.user_config_path()`) has a second, narrower way in: `user_form_fields`,
`propose_user` and `save_user`, for the page's Your settings. It takes form changes only (no whole text), and only
to the personal keys of `USER_FIELD_KEYS`; `hitl.notify` (it runs commands), `agents.*.user_settings` (it widens
what an agent reads) and `dashboard.host` are never on it. Clearing a field (`null`) removes the key, so the pack or
the engine decides again. The same text edits keep the file's comments and anchors, the same SHA-256 check refuses
a stale base, and the rest of the file is held: once every allow-listed key is taken out, the old and the new text
parse to the same data. A new `workspace.roots` entry must be a folder that exists. The candidate text must load as
the user file (`layers.load_user_settings`) and with every live repository's pack that loads with the file on disk
(`load_config(..., user_file=...)`), and there `validate` must find no error at a key that had none with the file on
disk; an error names that repository. A symlink, a hardlink, anything that is
not a regular file, or a symlinked `agentflow` folder is never read through or written: the form shows why, read
only. A new file is created private (`0o600`), in a folder made by `safeio.ensure_dir`.
"""

from __future__ import annotations

import copy
import difflib
import errno
import hashlib
import math
import os
import stat
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from agentflow import layers, safeio
from agentflow.config import (
    CACHE_TTLS, CLAIM_REFS, CLAIM_STALE_H_MAX, KNOWN_EFFORT_LEVELS, QUESTION_KINDS, ROUTE_MODES, Config, ConfigError,
    Issue, load_config, validate,
)
from agentflow.layers import PHASE_FREE_KEYS
from agentflow.vuln import SEVERITIES as VULN_SEVERITIES

__all__ = ["USER_FIELD_KEYS", "Conflict", "EditError", "Proposal", "Refused", "form_fields", "propose",
           "propose_user", "read", "read_user", "save", "save_user", "user_form_fields"]

MAX_TEXT = layers.MAX_CONFIG_BYTES
MERGE_TAG = "tag:yaml.org,2002:merge"
_NULL_TAG = "tag:yaml.org,2002:null"
AGENT_FREE_KEYS = ("enabled", "models", "max_turns", "prices", "cache_ttl", "lean", "user_settings", "read_only_roles",
                  "max_turns_by_role", "session_budget_usd", "bash_output_chars")  # what a page may change in an `agents.<name>` block
_PROTECTED_KEYS = (  # top-level blocks and dotted keys: commands, fences, and paths or refs the runner reads or writes
    "version", "engine", "gates", "gates_confirmed", "gate_sets", "guardrails", "conditions", "overlays", "project",
    "phase_approval", "record_step", "resolve_step", "retro_step", "paths", "defaults.env", "defaults.env_inherit", "context.autocompact_env", "context.progress_file",
    "hitl.notify", "dashboard.host", "work_source", "skills", "starter", "policy",
    "route.lanes", "route.profiles", "autonomy.approvals", "autonomy.ramp_samples",
    "vuln.source", "vuln.probe", "vuln.forbid", "vuln.dependency_review", "workspace",
)
PROTECTED = (
    *_PROTECTED_KEYS,
    "agents.<name> (all but enabled, models, max_turns, prices, cache_ttl, lean, user_settings, read_only_roles, "
    "max_turns_by_role, session_budget_usd, bash_output_chars)",
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
    if path.stat().st_size > MAX_TEXT:
        raise EditError(f"{path.name} is larger than {MAX_TEXT} bytes; fix it in git")
    text = path.read_text(encoding="utf-8")
    return text, sha256(text)


# --------------------------------------------------------------------------- the form's fields

# (section, key, label, kind, extra). A key whose parent block is missing from the file is left out of the form.
_INT = {"kind": "int", "min": 0}
_POS = {"kind": "int", "min": 1}
_NUM = {"kind": "number", "min": 0}
_PCT = {"kind": "int", "min": 1, "max": 100}

_SECTIONS: list[tuple[str, str, list[tuple[tuple[str, ...], str, dict]]]] = [
    ("Run defaults", "A running run picks these up when its next phase starts; the budgets are set when a run starts.", [
        (("defaults", "agent"), "Default agent", {"kind": "enum", "options": "@agents"}),
        (("defaults", "step_timeout_min"), "Step timeout (min)", _POS),
        (("defaults", "gate_timeout_min"), "Gate timeout (min)", {"kind": "number", "min": 0, "exclusive_min": True}),
        (("defaults", "fix_attempts"), "Fix attempts per gate failure", _INT),
        (("defaults", "run_budget_usd"), "Soft budget (USD, new runs): notify only",
         {"kind": "number", "min": 0, "exclusive_min": True}),
        (("defaults", "run_budget_hard_usd"), "Hard budget (USD, new runs): the run stops for approval",
         {"kind": "number", "min": 0, "exclusive_min": True}),
        (("defaults", "task_budget_usd"), "Budget per plan task (USD, 0 = off)", {"kind": "number", "min": 0}),
    ]),
    ("Recovery", "How long the runner keeps trying on its own after an outage or a failed call.", [
        (("recovery", "give_up_h"), "Give up after (h)", _NUM),
        (("recovery", "failed_attempts"), "Failed attempts", _POS),
    ]),
    ("Environment", "Checks that keep a run from waiting on the machine.", [
        (("preflight", "enabled"), "Check the machine before a run starts", {"kind": "bool"}),
        (("watchdog", "no_progress_min"), "End a session with no progress after (min, 0 = off)",
         {"kind": "number", "min": 0, "max": 1440}),
        (("watchdog", "read_only_idle_min"),
         "End a read-only session that is quiet with no tool running after (min, 0 = as above)",
         {"kind": "number", "min": 0, "max": 1440}),
    ]),
    ("Parallel build", "Plan tasks that can be built at the same time by one session's sub-agents.", [
        (("parallel", "max_subagents"), "Plan tasks a wave builds at once (1 = off, at most 6)",
         {"kind": "int", "min": 1, "max": 6}),
        (("parallel", "serial_paths"), "Paths that never go in a wave", {"kind": "list"}),
    ]),
    ("Several runs at once", "Slots give each live run its own number for ports and databases ({slot}); the queue "
                             "leaves some for you.", [
        (("slots", "count"), "Slots (1 = off, at most 16)", {"kind": "int", "min": 1, "max": 16}),
        (("queue", "human_slot"), "Slots the queue keeps free for a run you start",
         {"kind": "int", "min": 0, "max": 16}),
    ]),
    ("Usage window", "When runs stop starting new work because the plan's usage window is nearly spent; the soft mark "
                     "is your headroom.", [
        (("usage", "governor"), "Governor", {"kind": "enum", "options": ["on", "observe", "off"]}),
        (("usage", "five_hour_soft"), "5-hour soft mark: no new run or phase above it",
         {"kind": "number", "min": 0, "max": 1, "exclusive_min": True}),
        (("usage", "five_hour_hard"), "5-hour hard mark: no new session above it",
         {"kind": "number", "min": 0, "max": 1, "exclusive_min": True}),
        (("usage", "seven_day_soft"), "7-day soft mark: no new run or phase above it",
         {"kind": "number", "min": 0, "max": 1, "exclusive_min": True}),
        (("usage", "finish_first"), "Held runs go again closest to finish first", {"kind": "bool"}),
    ]),
    ("Effort", "How the runner sizes each step's reasoning effort.", [
        (("effort", "sizing"), "Size each step before it starts", {"kind": "bool"}),
        (("effort", "default"), "Level when sizing is off or fails", {"kind": "enum", "options": list(KNOWN_EFFORT_LEVELS)}),
        (("effort", "allowed"), "Levels a step may run at", {"kind": "list", "options": list(KNOWN_EFFORT_LEVELS)}),
    ]),
    ("Context", "When sessions compact or roll over.", [
        (("context", "autocompact_pct"), "Auto-compact at (%)", _PCT),
        (("context", "rollover_pct"), "Roll over at (%)", _PCT),
        (("context", "max_rollovers"), "Max rollovers", _INT),
        (("context", "max_compactions"), "Max compactions per session", _POS),
        (("context", "context_window_tokens"), "Context window (tokens)", {"kind": "int", "min": 100000}),
        (("context", "default_window_tokens"), "Default window (tokens)", _POS),
        (("context", "gate_output_lines"), "Gate output lines", _POS),
        (("context", "run_brief"), "Stable text in a cached system prompt (run brief)", {"kind": "bool"}),
        (("context", "resume_gate_lines"), "Gate lines in a fix prompt", {"kind": "int", "min": 10, "max": 1000}),
    ]),
    ("Memory", "What runs learn from earlier runs.", [
        (("memory", "lessons_max_tokens"), "Lessons per prompt (tokens, 0 = off)", {"kind": "int", "min": 0, "max": 4000}),
        (("memory", "digest_tokens"), "Retro digest size (tokens)", {"kind": "int", "min": 1000, "max": 50000}),
        (("memory", "retro_every_days"), "Days between retros (0 = manual only)", {"kind": "int", "min": 0, "max": 90}),
    ]),
    ("Issues", "What a run started from an issue does on GitHub.", [
        (("issues", "in_progress_label"), "In-progress label (empty = off)", {"kind": "text", "allow_empty": True}),
        (("issues", "comment"), "Comment when a run starts", {"kind": "bool"}),
        (("issues", "epic_labels"), "Epic labels (these get Refs, never Closes)", {"kind": "list"}),
        (("issues", "sync_label"), "Sync open issues with this label to Backlog (* = every open issue, empty = off)",
         {"kind": "text", "allow_empty": True}),
        (("issues", "sync_lane"), "Lane for a synced issue no lane label matches", {"kind": "enum", "options": "@lanes"}),
        (("issues", "lane_labels"), "Lane by issue label (first match wins)", {"kind": "map", "value_kind": "text"}),
    ]),
    ("Vulnerabilities", "How the vuln lane takes alerts and which files count as dependency files.", [
        (("vuln", "min_severity"), "Lowest severity taken (lower alerts are refused)",
         {"kind": "enum", "options": list(VULN_SEVERITIES)}),
        (("vuln", "sync"), "Open alerts become Backlog cards", {"kind": "bool"}),
        (("vuln", "dependency_files"), "Dependency files (globs; changes outside them are named in the PR)", {"kind": "list"}),
    ]),
    ("Autonomy", "What the runner settles by itself before a human hears of it.", [
        (("autonomy", "non_blocking_severities"), "Non-blocking severities", {"kind": "list"}),
        (("autonomy", "finding_rounds"), "Finding rounds", _POS),
        (("autonomy", "answer_recommended"), "Take recommended answers", {"kind": "bool"}),
        (("autonomy", "human_question_kinds"), "Question kinds only a human answers",
         {"kind": "list", "options": list(QUESTION_KINDS)}),
        (("autonomy", "revert_forbidden"), "Forbidden-path reverts per step", _INT),
        (("autonomy", "resolve_kinds"), "Resolver sessions per stop kind", {"kind": "map", "value_kind": "int", "min": 1}),
        (("autonomy", "speculate"), "Phases run while the approval before them waits", {"kind": "list"}),
    ]),
    ("Route", "How much ceremony a run's route gets, sized at its start.", [
        (("route", "mode"), "Route profiles", {"kind": "enum", "options": list(ROUTE_MODES)}),
    ]),
    ("CI", "The `ci` step in ship.", [
        (("ci", "poll_s"), "Poll every (s)", _POS),
        (("ci", "appear_min"), "Checks must appear within (min)", _NUM),
        (("ci", "timeout_min"), "Pending timeout (min)", _NUM),
        (("ci", "fix_rounds"), "Fix rounds", _INT),
        (("ci", "rerun_rounds"), "Rerun rounds", _INT),
        (("ci", "watch_max_h"), "Watch pending checks after the timeout for (h, 0 = stop)",
         {"kind": "number", "min": 0, "max": 720}),
        (("ci", "early_pr_after"), "Open the draft PR after phase (empty = at finish)",
         {"kind": "text", "allow_empty": True}),
    ]),
    ("Human in the loop", "Reminders and parking.", [
        (("hitl", "remind_after_h"), "Remind after (h)", _NUM),
        (("hitl", "park_independent"), "Park a stopped task and go on", {"kind": "bool"}),
        (("hitl", "park_kinds"), "Stops that park a task", {"kind": "list"}),
        (("hitl", "digest"), "One digest line instead of a notification per stop", {"kind": "bool"}),
    ]),
    ("Dashboard", "The page reads these at once; a new port needs a dashboard restart.", [
        (("dashboard", "port"), "Port (restart)", {"kind": "int", "min": 0, "max": 65535}),
        (("dashboard", "show_done_days"), "Show done cards for (days, 0 = always)", _INT),
        (("dashboard", "done_shown"), "Done cards shown before Show all", {"kind": "int", "min": 1, "max": 1000}),
        (("dashboard", "board_refresh_s"), "Refresh the Board at most every (s)", {"kind": "number", "min": 0.5, "max": 60}),
        (("dashboard", "artifact_max_kb"), "Largest spec, plan or diff the page loads (KB)", {"kind": "int", "min": 16, "max": 8192}),
        (("dashboard", "inbox_legacy"), "Also show the old Needs-human list at the bottom of the Board", {"kind": "bool"}),
        (("dashboard", "agents", "show_exited_min"), "Show exited agents for (min)", _INT),
        (("dashboard", "agents", "idle_warn_min"), "Warn when idle for (min)", _INT),
        (("dashboard", "backlog", "sync_base"), "Fast-forward the base branch", {"kind": "bool"}),
        (("dashboard", "backlog", "module_lane"), "Lane for map modules", {"kind": "enum", "options": "@lanes"}),
        (("dashboard", "backlog", "lane_labels"), "Lane labels", {"kind": "map", "value_kind": "text"}),
        (("backlog", "ignore_specs"), "Spec files that are not backlog items (globs under the specs folder)",
         {"kind": "list"}),
        (("backlog", "claims"), "Claim an item on origin when a run starts it", {"kind": "bool"}),
        (("backlog", "claim_ref"), "Where a claim lives on origin", {"kind": "enum", "options": list(CLAIM_REFS)}),
        (("backlog", "claim_stale_h"), "Show a claim as stale after (h)", {"kind": "int", "min": 1, "max": CLAIM_STALE_H_MAX}),
    ]),
]


def _get(data: Any, key: tuple[str, ...]) -> tuple[bool, Any]:
    for part in key:
        if not isinstance(data, dict) or part not in data:
            return False, None
        data = data[part]
    return True, data


@dataclass
class _Layers:
    """The file as written and the layered config it makes: the form shows the effective values (`merged`) and
    writes only into the project file (`project`)."""
    project: dict                                  # the pack file as written
    merged: dict                                   # every layer merged, before `@` resolution (`Config.merged`)
    origins: dict[tuple[str, ...], str]            # each leaf of `merged`: its layer (`Config.layer_origins`)
    below: dict = field(default_factory=dict)      # the layers below the project merged, as they apply to it
    user_path: Path | None = None                  # the user's own file, when it sets anything
    comments: dict[tuple[str, ...], str] = field(default_factory=dict)  # each key's line comment, any layer's


_RANK = {"engine": 0, "project": 2, "user": 3}  # a starter (`starter:<name>`) sits between the engine and the pack


def _layers(path: Path, text: str) -> _Layers:
    """The layered view of the pack at `path`, whose text is `text`. A user file the pack refuses is left out; a
    pack that does not load at all falls back to the file alone, as the form was before the layers."""
    try:  # every walk below copies each alias out: refuse an unbounded file before any of them
        project = layers.parse_config(text) or {}
    except layers.ConfigRefused as refused:
        raise EditError(f"{path.name} {refused}; fix it in git") from refused
    project = project if isinstance(project, dict) else {}
    comments = _comments(text)
    try:
        cfg = load_config(path)
    except ConfigError:
        try:  # a user file that does not load against this pack: the engine and the pack still do
            cfg = load_config(path, user=False)
        except ConfigError:
            return _Layers(project, project, {key: "project" for key in layers.leaf_keys(project)}, comments=comments)
    stack = layers.base_layers(cfg.merged.get("starter"))  # the engine defaults, then the pack's starter
    below, _ = layers.merge(stack)
    if layers.owns_workflow(project):  # the pack writes the whole workflow: none of the engine's applies to it
        for key in layers.WORKFLOW_KEYS:
            below.pop(key, None)
    user_path = layers.user_config_path() if "user" in cfg.layer_origins.values() else None
    every: dict[tuple[str, ...], str] = {}
    for layer in stack:  # bottom-up: a higher layer's comment on the same key wins
        every.update(_comments(layer.path.read_text(encoding="utf-8")))
    every.update(comments)
    if user_path is not None:
        try:
            every.update(_comments(user_path.read_text(encoding="utf-8")))
        except OSError:
            pass
    return _Layers(project, cfg.merged, cfg.layer_origins, below, user_path, every)


def _under(origins: dict[tuple[str, ...], str], key: tuple[str, ...]) -> list[str]:
    return [origin for leaf, origin in origins.items() if leaf[:len(key)] == key]


def _source(lay: _Layers, key: tuple[str, ...]) -> str:
    """The layer a field's value comes from; a mapping's, the highest layer among its entries. A key no layer
    sets is the project's to set."""
    under = _under(lay.origins, key)
    if not under or not _get(lay.merged, key)[0]:
        return "project"
    return max(under, key=lambda origin: _RANK.get(origin, 1))


def _catalog(raw: dict) -> list[dict]:
    """The form's sections, each with its fields' definitions (no values yet)."""
    sections = []
    for title, note, rows in _SECTIONS:
        fields = [{"key": list(key), "label": label, **extra} for key, label, extra in rows]
        sections.append({"title": title, "note": note, "fields": fields})
    for name, spec in (raw.get("agents") or {}).items():
        if not isinstance(spec, dict):
            continue
        fields = [
            {"key": ["agents", name, "enabled"], "label": "Enabled", "kind": "bool"},
            {"key": ["agents", name, "models"], "label": "Model by role", "kind": "map", "value_kind": "text"},
            {"key": ["agents", name, "max_turns"], "label": "Max turns", "kind": "int", "min": 1},
        ]
        if name == "claude":  # the cache and session keys are read by the claude agent alone
            fields += [
                {"key": ["agents", name, "cache_ttl"], "label": "Prompt-cache lifetime", "kind": "enum",
                 "options": list(CACHE_TTLS)},
                {"key": ["agents", name, "lean"], "label": "Lean sessions (no project MCP servers)", "kind": "bool"},
                {"key": ["agents", name, "user_settings"], "label": "Read my own settings and agents", "kind": "bool"},
                {"key": ["agents", name, "read_only_roles"], "label": "Read-only roles (no edits, no tests)", "kind": "list"},
                {"key": ["agents", name, "max_turns_by_role"], "label": "Max turns by role (0 = no cap)", "kind": "map",
                 "value_kind": "int", "min": 0},
                {"key": ["agents", name, "session_budget_usd"], "label": "Spend cap per session by role (USD, 0 = no cap)",
                 "kind": "map", "value_kind": "number", "min": 0},
                {"key": ["agents", name, "bash_output_chars"], "label": "Shell output cap (chars, 0 = the CLI's own)",
                 "kind": "int", "min": 0, "max": 1000000},
            ]
        sections.append({"title": f"Agent: {name}", "note": "Models by role, and the turn limit for the roles "
                         "the turn map by role does not name. Its command line is protected.", "fields": fields})
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


_INHERITED_ENTRY = "An entry from a lower layer can't be removed here: write `<name>: null` for it in the YAML tab."


def form_fields(path: Path) -> dict:
    """What the Settings page shows: the file's text and hash, the form (each field with its effective value, the
    layer it comes from, whether the file states it, and the line comment on it in any layer's file), and the
    protected settings with the layer each comes from when that is not the pack."""
    text, digest = read(path)
    lay = _layers(path, text)
    index = _field_index(lay)
    sections = []
    for section in _catalog(lay.merged):
        fields = []
        for fdef in section["fields"]:
            key = tuple(fdef["key"])
            parent_ok, _ = _get(lay.merged, key[:-1])
            if not parent_ok:
                continue
            _, value = _get(lay.merged, key)
            item = {**index[key], "value": value, "present": _get(lay.project, key)[0], "source": _source(lay, key),
                    "help": lay.comments.get(key, "")}
            if item["kind"] == "map" and any(_RANK.get(o, 1) < _RANK["project"] for o in _under(lay.origins, key)):
                item["help"] = f"{item['help'].rstrip('.')}. {_INHERITED_ENTRY}" if item["help"] else _INHERITED_ENTRY
            if not item.get("readonly") and isinstance(value, str) and value.startswith("@"):
                item["readonly"] = "a reference to another key; change it in the YAML tab"
            item["target"] = "project"
            fields.append(item)
        if fields:
            sections.append({**section, "fields": fields})
    sources = {dotted: _source(lay, tuple(dotted.split("."))) for dotted in _PROTECTED_KEYS}
    return {"path": str(path), "sha256": digest, "text": text, "sections": sections, "protected": list(PROTECTED),
            "protected_sources": {dotted: src for dotted, src in sources.items() if src != "project"}}


def _field_index(lay: _Layers) -> dict[tuple[str, ...], dict]:
    """Every field the form may hold, with its options and, for a value the user's own file sets (or a mapping
    with an entry from it), why it is read-only: the page never writes a personal value into the pack."""
    index = {}
    for section in _catalog(lay.merged):
        for fdef in section["fields"]:
            fdef = {**fdef}
            options = _options(fdef, lay.merged)
            if options is not None:
                fdef["options"] = options
            key = tuple(fdef["key"])
            if "user" in _under(lay.origins, key):
                fdef["readonly"] = f"set in your user config ({lay.user_path}), which overrides the pack"
                if key in USER_FIELD_KEYS:
                    fdef["hint"] = "change it under Your settings"
            index[key] = fdef
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
        empty_ok = kind == "text" and fdef.get("allow_empty") and value == ""
        if not isinstance(value, str) or not (value.strip() or empty_ok) or "\n" in value or len(value) > 500:
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
        new_text = _apply_form(current, changes, path)
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
    """Load and validate `new_text` beside `path` (so `prompts/` resolves), and hold the protected settings.

    The protected settings are compared after `@` resolution, once per way both the file on disk and the new text
    load: without the user's own file, and with it. Each pair sees the same engine defaults and the same user file
    on both sides, so a change that only a reference reaches still shows. A user file that does not load against
    the file on disk (it names another repository's agent) leaves the user-less pair, and a pack on disk that loads
    only with the user file leaves the other: then the new pack's protected values must also be the same with and
    without the user file, and they are held as written too. A new text that loads only with the user file is an error: it would
    break for a teammate and for CI. When no pair loads (the file on disk loads neither way, or only the way the
    new text does not), the protected settings are held as written: a broken file can be repaired here, but not
    by changing a protected setting."""
    try:  # refused before any load or comparison: each one copies every alias out
        parsed = layers.parse_config(new_text)
    except layers.ConfigRefused as refused:
        return [Issue("error", "", f"config.yaml {refused}")]
    except yaml.YAMLError as exc:
        return [Issue("error", "", f"not valid YAML: {exc}")]
    if not isinstance(parsed, dict):
        return [Issue("error", "", "the file must hold a mapping at the top level")]
    fd, temp = tempfile.mkstemp(prefix=".config-check-", suffix=".yaml", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(new_text)
        try:
            new_cfg = load_config(Path(temp))  # validated as a run would load it: with the user's own file
        except ConfigError as exc:
            _hold_unloadable(path, parsed)  # a pack format or engine change does not load: it is still refused
            return [Issue("error", exc.path, exc.message)]
        needs_user: ConfigError | None = None
        try:
            new_alone: dict | None = load_config(Path(temp), user=False).raw
        except ConfigError as exc:
            needs_user, new_alone = exc, None
        from agentflow.guards import settings_coverage

        issues = validate(new_cfg, repo) + settings_coverage(new_cfg, repo)
    finally:
        Path(temp).unlink(missing_ok=True)
    old_alone, old_user = _raw_or_none(path, user=False), _raw_or_none(path, user=True)
    pairs = [(old, new) for old, new in ((old_alone, new_alone), (old_user, new_cfg.raw))
             if old is not None and new is not None]
    changed = [key for old, new in pairs for key in _protected_changes(old, new)]
    if not pairs:  # the file on disk loads neither way, or only the way the new text does not: hold it as written
        changed += _written_changes(path, parsed)
    if old_alone is None and old_user is not None and new_alone is not None:
        # The file on disk needs the user file, so only the pair with it was compared, and there a key the user
        # file overrides resolves to the user's value on both sides. The new pack must not make a protected value
        # depend on the personal file (`hitl.notify` is itself personal), and it is also held as written.
        changed += [key for key in _protected_changes(new_alone, new_cfg.raw) if key != "hitl.notify"]
        changed += _written_changes(path, parsed)
        if _get(new_alone, ("hitl", "notify")) != _get(parsed, ("hitl", "notify")):  # a reference could change it
            changed.append("hitl.notify")
    changed = list(dict.fromkeys(changed))
    if changed:
        raise Refused("these settings are protected and can only be changed in the file, in git: " + ", ".join(changed))
    if needs_user is not None:
        where = f" ({needs_user.path}: {needs_user.message})" if needs_user.path else f" ({needs_user.message})"
        return [Issue("error", needs_user.path, "the pack must load without your personal user file: it would break "
                      "for teammates and CI" + where), *issues]
    return issues


def _written_changes(path: Path, parsed: dict) -> list[str]:
    """The protected settings `parsed` changes compared with the file on disk, both as written (no layers, no
    `@` resolution)."""
    try:
        text = layers.read_config_text(path)
        old = layers.parse_config(text) if text is not None else None
    except (OSError, yaml.YAMLError, RecursionError, ValueError):  # unreadable, refused or not YAML: nothing to hold
        return []
    return _protected_changes(old if isinstance(old, dict) else {}, parsed)


def _raw_or_none(path: Path, *, user: bool) -> dict | None:
    try:
        return load_config(path, user=user).raw
    except ConfigError:
        return None


def _hold_unloadable(path: Path, parsed: dict) -> None:
    """Refuse a text that changes a protected setting even when it no longer loads, comparing it as written with
    the file as written (a loaded config's `@` references are resolved, the new text's are not). It holds whatever
    the file on disk loads: one that loads neither way can be repaired, but not by changing a protected setting."""
    changed = _written_changes(path, parsed)
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


def _changes_list(changes: Any) -> list:
    if not isinstance(changes, list) or not changes:
        raise EditError("changes must be a non-empty list")
    return changes


def _apply_form(text: str, changes: Any, path: Path) -> str:
    """`text` (the pack at `path`) with the form's `changes` written into it (`_apply_changes`)."""
    _changes_list(changes)
    lay = _layers(path, text)
    return _apply_changes(text, changes, lay, _field_index(lay))


def _apply_changes(text: str, changes: Any, lay: _Layers, index: dict[tuple[str, ...], dict]) -> str:
    """`text` (the file `lay.project` was parsed from) with the form's `changes` written into it. A key must be in
    `index` and its value must fit that field. A field is checked against the layered config, and only what the file
    itself must state is written: a mapping's entries that differ from the layers below it, or that the file already
    states. The new text must parse back to exactly the intended data."""
    _changes_list(changes)
    expected = copy.deepcopy(lay.project)
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"key", "value"}:
            raise EditError("each change is an object with exactly key and value")
        key = change["key"]
        if not isinstance(key, list) or not all(isinstance(k, str) for k in key) or tuple(key) not in index:
            raise Refused(f"{key!r} is not a setting this page can change; use the YAML tab")
        fdef = index[tuple(key)]
        if fdef.get("readonly"):
            raise Refused(f"{'.'.join(key)} cannot be changed here: {fdef['readonly']}")
        present, old_value = _get(lay.merged, tuple(key))  # an inherited reference is one too
        if present and isinstance(old_value, str) and old_value.startswith("@"):
            raise Refused(f"{'.'.join(key)} is a reference to another key; change it in the YAML tab")
        problem = _check_value(fdef, change["value"])
        if problem:
            raise EditError(problem)
        value = change["value"]
        if fdef["kind"] == "map" and value is not None:
            value = _own_entries(lay, tuple(key), value)
            if value is None:  # every entry is what the layers below already say: nothing to write
                continue
        if value is None:
            text = _edit(text, tuple(key), _DELETE)
            _drop(expected, tuple(key))
        else:
            text = _set_deep(text, tuple(key), value)
            _put(expected, tuple(key), value)
    if (yaml.safe_load(text) or {}) != expected:
        raise EditError("the change could not be made while keeping the file's layout; make it in the YAML tab")
    return text


def _own_entries(lay: _Layers, key: tuple[str, ...], value: dict) -> dict | None:
    """The entries of a mapping the pack must state: those it states already, and those whose value differs from
    the merge of the layers below it. None when the pack states no such mapping and there is nothing to add."""
    _, below = _get(lay.below, key)
    below = below if isinstance(below, dict) else {}
    stated, mine = _get(lay.project, key)
    mine = mine if isinstance(mine, dict) else {}
    own = {name: entry for name, entry in value.items()
           if name in mine or name not in below or type(below[name]) is not type(entry) or below[name] != entry}
    # a `null` the pack states deletes an inherited entry, which the page therefore never shows: keep it deleted
    own |= {name: None for name, entry in mine.items() if entry is None and name not in value}
    return own if own or stated else None


def _put(data: dict, key: tuple[str, ...], value: Any) -> None:
    for part in key[:-1]:
        if data.get(part) is None:  # a missing parent, or a null one (`workspace:` with only comments under it)
            data[part] = {}
        data = data[part]
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
    """One text edit: replace, add (with any missing parents) or delete (`_DELETE`) the value at `key`. A document
    with no node (empty, or comments only) takes the key on a new line at its end, and a parent whose value is null
    (a key with nothing, or only comments, under it) takes the rest of the key as a flow mapping."""
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    if root is None:
        if value is _DELETE:
            return text
        nested: Any = value
        for inner in reversed(key[1:]):
            nested = {inner: nested}
        lead = "" if not text or text.endswith("\n") else "\n"
        return f"{text}{lead}{_render(key[0])}: {_render(nested)}\n"
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
            nested = value
            for inner in reversed(rest):
                nested = {inner: nested}
            return _add(text, node, part, nested)
        index, knode, vnode = found
        if last:
            if id(vnode) in shared:
                raise EditError(f"{'.'.join(key)} is anchored and used elsewhere; change it in the YAML tab")
            if value is _DELETE:
                return _delete(text, node, index)
            return _replace(text, vnode, value, knode)
        if not isinstance(vnode, MappingNode):
            if value is _DELETE:
                return text
            if isinstance(vnode, ScalarNode) and vnode.tag == _NULL_TAG and id(vnode) not in shared:
                nested = value
                for inner in reversed(key[depth + 1:]):
                    nested = {inner: nested}
                return _replace(text, vnode, nested)
            raise EditError(f"{'.'.join(key[:depth + 1])} is not a mapping")
        node = vnode
    return text


def _replace(text: str, node: Node, value: Any, key: Node | None = None) -> str:
    start, end = node.start_mark.index, _last_end(node)
    if (key is not None and isinstance(node, (MappingNode, SequenceNode)) and not node.flow_style
            and node.start_mark.line > key.end_mark.line and node.start_mark.column <= key.start_mark.column):
        # A block value no deeper than its key (`roots:` then `- a` in the key's column, as PyYAML writes a list):
        # a one-line value can't stand where its first entry did, so it goes right after the key's colon, and the
        # comments between the two lines stay.
        colon = text.find(":", key.end_mark.index, start)
        if colon == -1:
            raise EditError("cannot find the colon after a key")
        return text[:colon + 1] + " " + _render(value) + text[colon + 1:_line_start(text, start) - 1] + text[end:]
    gap = " " if isinstance(node, ScalarNode) and start == end else ""  # an implicit null, right after its colon
    return text[:start] + gap + _render(value) + text[end:]


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


# --------------------------------------------------------------------------- the user file: Your settings

_LINES = {"kind": "list", "lines": True}  # one entry per line on the page
_MODELS = {"kind": "map", "value_kind": "text"}
# (title, note, rows). A row with no label and no extra takes both from the pack form's `_SECTIONS`, so a key the
# pack form already has is defined once. These rows are the allow-list (`USER_FIELD_KEYS`): nothing else is written.
_USER_SECTIONS: list[tuple[str, str, list[tuple[tuple[str, ...], str | None, dict | None]]]] = [
    ("Workspace", "The folders whose repositories the dashboard lists; a change takes effect at once.", [
        (("workspace", "roots"), "Folders that hold repositories (absolute or ~/ paths)", _LINES),
        (("workspace", "repos"), "Single repositories (absolute or ~/ paths)", _LINES),
        (("workspace", "exclude"), "Leave out (a path, or a bare folder name)", _LINES),
    ]),
    ("Across repositories", "Machine-wide, read by each run when it needs them; no restart.", [
        (("workspace", "max_active_runs"),
         "Most runs working at once on this machine: agent sessions and gates (empty = no cap)",
         {"kind": "int", "min": 1, "max": layers.MAX_ACTIVE_RUNS}),
        (("workspace", "digest"),
         "One notification line over every repository (replaces each repository's digest)", {"kind": "bool"}),
    ]),
    ("Dashboard", "The page reads these at once; a new port needs a dashboard restart.", [
        (("dashboard", "port"), None, None),
        (("dashboard", "board_refresh_s"), None, None),
        (("dashboard", "done_shown"), None, None),
    ]),
    ("Budgets", "Your own budgets, over every repository's; they are set when a run starts.", [
        (("defaults", "run_budget_usd"), None, None),
        (("defaults", "run_budget_hard_usd"), None, None),
        (("defaults", "task_budget_usd"), None, None),
    ]),
    ("Usage window", "When runs stop starting new work because the plan's usage window is nearly spent; the soft mark "
                     "is your headroom.", [
        (("usage", "governor"), None, None),
        (("usage", "five_hour_soft"), None, None),
        (("usage", "five_hour_hard"), None, None),
        (("usage", "seven_day_soft"), None, None),
        (("usage", "finish_first"), None, None),
    ]),
    ("Reminders", "When a run that waits on you reminds you again.", [
        (("hitl", "remind_after_h"), None, None),
    ]),
    ("Models", "Your model for each role, by agent; a role you leave out keeps the pack's or the engine's.", [
        (("agents", "claude", "models"), "claude: model by role", _MODELS),
        (("agents", "codex", "models"), "codex: model by role", _MODELS),
    ]),
]
USER_FIELD_KEYS: tuple[tuple[str, ...], ...] = tuple(key for _, _, rows in _USER_SECTIONS for key, _, _ in rows)
_PACK_FIELDS = {key: (label, extra) for _, _, rows in _SECTIONS for key, label, extra in rows}
_USER_CONFLICT = "your user config changed since the page loaded it; reload the Settings page and redo the change"


def _user_catalog() -> list[dict]:
    """Your settings' sections, each with its fields' definitions (no values yet)."""
    sections = []
    for title, note, rows in _USER_SECTIONS:
        fields = []
        for key, label, extra in rows:
            if label is None or extra is None:
                label, extra = _PACK_FIELDS[key]
            fields.append({"key": list(key), "label": label, **extra, "optional": True, "target": "user"})
        sections.append({"title": title, "note": note, "fields": fields})
    return sections


def _user_path(path: Path | None) -> Path:
    if path is not None:
        return Path(path)
    try:
        return layers.user_config_path()
    except RuntimeError as exc:  # no home directory to hold the file
        raise EditError("there is no home directory to hold your user config") from exc


def read_user(path: Path) -> tuple[str, str, bool]:
    """`(text, sha256, exists)` of the user file; a missing one is `("", sha256(""), False)`. It is opened without
    following a symlink, and a symlink (at the file or its `agentflow` folder), a hardlink, anything that is not a
    regular file, a file over `MAX_TEXT` bytes or text that is not UTF-8 is an `EditError`: the page never reads
    through, or writes, any of them."""
    path = Path(path)
    fix = "the dashboard edits only a regular file in a real folder; fix it by hand"
    if path.parent.is_symlink():
        raise EditError(f"{path.parent} is a symlink; {fix}")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return "", sha256(""), False
    except OSError as exc:
        if exc.errno == errno.ELOOP or path.is_symlink():
            raise EditError(f"{path} is a symlink; {fix}") from exc
        raise EditError(f"cannot read {path}: {exc.strerror or exc}") from exc
    try:  # checked on the raw descriptor: wrapping a folder's in a file object fails before any check
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise EditError(f"{path} is not a regular file; {fix}")
        if info.st_nlink > 1:
            raise EditError(f"{path} has another name (a hardlink); {fix}")
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(MAX_TEXT + 1)
    if len(data) > MAX_TEXT:
        raise EditError(f"{path} is larger than {MAX_TEXT} bytes; fix it by hand")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EditError(f"{path} is not UTF-8 text; fix it by hand") from exc
    return text, sha256(text), True


def _parse_user(text: str, path: Path) -> dict:
    """The user file's text as a mapping (an empty file is `{}`); `EditError` when it is not one."""
    try:
        parsed = layers.parse_config(text)
    except layers.ConfigRefused as refused:
        raise EditError(f"{path} {refused}; fix it by hand") from refused
    except yaml.YAMLError as exc:
        raise EditError(f"{path} is not valid YAML; fix it by hand: {exc}") from exc
    except RecursionError as exc:
        raise EditError(f"{path} is nested too deeply to read; fix it by hand") from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise EditError(f"{path} must hold a mapping at the top level; fix it by hand")
    return parsed


def _user_layers(path: Path, text: str, data: dict) -> tuple[_Layers, str | None]:
    """The user file over the engine defaults (no pack, and no `check_user`, so a file that does not load still
    shows its values): `project` is the file, `below` the engine. A file `merge` refuses shows the engine's values,
    and the second item says why."""
    stack = layers.base_layers(None)
    below, below_origins = layers.merge(stack)
    comments: dict[tuple[str, ...], str] = {}
    for layer in stack:
        comments.update(_comments(layer.path.read_text(encoding="utf-8")))
    comments.update(_comments(text))
    problem = None
    try:
        merged, origins = layers.merge([*stack, layers.Layer("user", data, path)])
    except layers.LayerError as exc:
        merged, origins, problem = below, below_origins, f"{path}: {exc}"
    return _Layers(data, merged, origins, below, path, comments), problem


def user_form_fields(path: Path | None = None) -> dict:
    """What Your settings shows: the user file's path, hash and whether it exists, and the allow-listed fields with
    their effective values (the engine defaults under the file). Never raises for a bad file: a file the page can't
    edit (a symlink, a hardlink, not YAML ...) is `editable` False with `error` saying why and every field read only;
    a mapping that does not load is editable, with the load error, so it can be fixed here."""
    where: Path | None = None
    text, digest, exists, editable, error = "", sha256(""), False, True, None
    data: dict = {}
    try:
        where = _user_path(path)
        text, digest, exists = read_user(where)
        data = _parse_user(text, where)
    except EditError as exc:
        editable, error, data = False, str(exc), {}
        exists = exists or (where is not None and os.path.lexists(where))
    if editable and where is not None:
        try:
            layers.load_user_settings(where)
        except layers.LayerError as exc:
            error = str(exc)
    lay, problem = _user_layers(where or Path("config.yaml"), text if editable else "", data)
    error = error or problem
    sections = []
    for section in _user_catalog():
        fields = []
        for fdef in section["fields"]:
            key = tuple(fdef["key"])
            present = _get(data, key)[0]
            item = {**fdef, "value": _get(lay.merged, key)[1], "present": present,
                    "source": "user" if present else "engine", "help": lay.comments.get(key, "")}
            if not editable:
                item["readonly"] = error
            fields.append(item)
        sections.append({**section, "fields": fields})
    return {"path": str(where) if where is not None else "", "sha256": digest, "exists": exists,
            "editable": editable, "error": error, "sections": sections}


def _is_folder(entry: str) -> bool:
    """One `stat`: `entry` (`~` expanded) is a folder that exists. Nothing in it is listed or read."""
    try:
        return Path(entry).expanduser().is_dir()
    except (OSError, RuntimeError):
        return False


def _prune_empty(text: str, key: tuple[str, ...]) -> str:
    """`text` with each parent of `key` that a removal left an empty mapping removed too, deepest first: a user file
    may hold personal settings and nothing else, so `agents: {claude: {}}` would not load."""
    for depth in range(len(key) - 1, 0, -1):
        present, parent = _get(yaml.safe_load(text) or {}, key[:depth])
        if not present or parent != {}:
            break
        text = _edit(text, key[:depth], _DELETE)
    return text


def _without_user_fields(data: dict) -> dict:
    """`data` with every allow-listed leaf taken out, and each parent on the way that is left empty or null."""
    out = copy.deepcopy(data)
    for key in USER_FIELD_KEYS:
        _drop(out, key)
        for depth in range(len(key) - 1, 0, -1):
            present, parent = _get(out, key[:depth])
            if not present or not (parent is None or parent == {}):
                break
            _drop(out, key[:depth])
    return out


UserPacks = Sequence[tuple[str, Path] | tuple[str, Path, Path]]


def propose_user(base_sha256: str, changes: list, packs: UserPacks = (), path: Path | None = None) -> Proposal:
    """The user-file text a save would write, with what stops it; raises `Conflict`, `Refused`, `EditError`.

    `packs` is `(repository name, pack config path, checkout)` per live repository; without the checkout it is the
    folder that holds the pack's `.agentflow/`. In order: the base must be the file's
    current hash; every change key must be in `USER_FIELD_KEYS` (else `Refused`, whatever the value); each value
    must fit its field, a `workspace.*` list must have the shape `layers.workspace_settings` takes, and a
    `workspace.roots` entry the file does not list yet must be a folder that exists; the edit must leave the rest of
    the file as it was (else `Refused`). Then the candidate must load as the user file, and with each pack that
    loads with the file on disk, and there `validate` must find no error at a key that had none with the file on disk
    (as a run's reload and `agentflow validate` would see it); an issue names that repository, and the real file,
    never the temporary one."""
    path = _user_path(path)
    current, digest, _ = read_user(path)
    if base_sha256 != digest:
        raise Conflict(_USER_CONFLICT)
    _changes_list(changes)
    allowed = set(USER_FIELD_KEYS)
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"key", "value"}:
            raise EditError("each change is an object with exactly key and value")
        key = change["key"]
        if not isinstance(key, list) or not all(isinstance(k, str) for k in key) or tuple(key) not in allowed:
            raise Refused(f"{key!r} is not a personal setting this page can change; edit your user config by hand")
    data = _parse_user(current, path)
    lay, _ = _user_layers(path, current, data)
    index = {tuple(f["key"]): f for section in _user_catalog() for f in section["fields"]}
    _, listed = _get(data, ("workspace", "roots"))
    listed = listed if isinstance(listed, list) else []
    for change in changes:
        key, value = tuple(change["key"]), change["value"]
        problem = _check_value(index[key], value)
        if problem:
            raise EditError(problem)
        if key[0] != "workspace":
            continue
        try:
            layers.workspace_settings({key[1]: value})
        except layers.LayerError as exc:
            raise EditError(str(exc)) from exc
        if key[1] == "roots":
            for entry in value or ():
                if entry not in listed and not _is_folder(entry):
                    raise EditError(f"workspace.roots: {entry!r} is not a folder")
    new_text = _apply_changes(current, changes, lay, index)
    for change in changes:
        if change["value"] is None:
            new_text = _prune_empty(new_text, tuple(change["key"]))
    if len(new_text.encode("utf-8")) > MAX_TEXT:
        raise EditError(f"your user config would be larger than {MAX_TEXT} bytes")
    if _without_user_fields(data) != _without_user_fields(_parse_user(new_text, path)):
        raise Refused("the change would touch more of your user config than its personal settings; edit it by hand")
    issues = _check_user_text(path, new_text, packs)
    diff = "".join(difflib.unified_diff(current.splitlines(keepends=True), new_text.splitlines(keepends=True),
                                        "config.yaml", "config.yaml (new)"))
    return Proposal(digest, new_text, diff, issues)


def _check_user_text(path: Path, new_text: str, packs: UserPacks) -> list[Issue]:
    """Load `new_text` as the user file, from a temporary file beside it (or in a private temporary folder while the
    file's folder does not exist yet: a preview creates nothing)."""
    if path.parent.is_dir() and not path.parent.is_symlink():
        return _check_candidate(path, new_text, packs, path.parent)
    with tempfile.TemporaryDirectory(prefix="agentflow-user-check-") as scratch:
        return _check_candidate(path, new_text, packs, Path(scratch))


def _check_candidate(path: Path, new_text: str, packs: UserPacks, folder: Path) -> list[Issue]:
    fd, temp = tempfile.mkstemp(prefix=".config-check-", suffix=".yaml", dir=folder)
    names = sorted({temp, os.path.realpath(temp)}, key=len, reverse=True)

    def said(text: str) -> str:  # the real file's path wherever a message names the temporary one
        for name in names:
            text = text.replace(name, str(path))
        return text

    issues: list[Issue] = []
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(new_text)
        try:
            layers.load_user_settings(Path(temp))
        except layers.LayerError as exc:
            issues.append(Issue("error", said(exc.path), said(exc.message)))
        for entry in packs:
            name, pack_path = entry[0], Path(entry[1])
            repo = Path(entry[2]) if len(entry) > 2 else pack_path.parent.parent
            try:
                now = load_config(pack_path, user_file=path)
            except (ConfigError, OSError):  # already broken with the file on disk: it blocks nothing
                continue
            try:
                cfg = load_config(pack_path, user_file=Path(temp))
            except (ConfigError, OSError) as exc:
                where, message = (exc.path, exc.message) if isinstance(exc, ConfigError) else ("", str(exc))
                issues.append(Issue("error", said(where), f"{name}: {said(message)}"))
                continue
            issues += [Issue("error", said(i.path), f"{name}: {said(i.message)}")
                       for i in _new_validate_errors(now, cfg, repo, said)]
    finally:
        Path(temp).unlink(missing_ok=True)
    return issues


def _new_validate_errors(now: Config, cfg: Config, repo: Path, said: Callable[[str], str]) -> list[Issue]:
    """The errors `validate` finds in `cfg` (the pack with the candidate user file) at a key where it finds none in
    `now` (the pack with the file on disk). A key keeps its error when only the message's values move (a soft budget
    raised past a hard limit that was already below it), and a pack `validate` can't check today blocks nothing."""
    try:
        before = {said(i.path) or said(i.message) for i in validate(now, repo) if i.level == "error"}
    except Exception:  # validate itself fails on the pack as it is: like a pack that does not load, it blocks nothing
        return []
    try:
        after = [i for i in validate(cfg, repo) if i.level == "error"]
    except Exception as exc:  # it fails only with the candidate: that is new
        return [Issue("error", "", said(f"validate failed with your new settings: {exc}"))]
    return [i for i in after if (said(i.path) or said(i.message)) not in before]


def save_user(proposal: Proposal, path: Path | None = None) -> str:
    """Write a checked user-file proposal; the new SHA-256. Raises `Conflict` if the file changed meanwhile and
    `EditError` for a proposal with errors or a file (or folder) `read_user` refuses. A new file is `0o600`, in a
    folder `safeio.ensure_dir` makes (refusing a symlink); an existing one keeps its mode."""
    if proposal.errors:
        raise EditError("the change does not load: " + "; ".join(f"{i.path}: {i.message}" for i in proposal.errors))
    path = _user_path(path)
    with _LOCK:
        _, digest, exists = read_user(path)
        if digest != proposal.base_sha256:
            raise Conflict(_USER_CONFLICT)
        mode = stat.S_IMODE(os.lstat(path).st_mode) if exists else safeio.FILE_MODE
        try:
            folder = safeio.ensure_dir(path.parent)
        except OSError as exc:  # `SymlinkRefused` among them
            raise EditError(f"cannot write your user config in {path.parent}: {exc.strerror or exc}") from exc
        fd, temp = tempfile.mkstemp(prefix=".config-", suffix=".yaml.tmp", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(proposal.text)
            os.chmod(temp, mode)
            os.replace(temp, path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise
    return sha256(proposal.text)
