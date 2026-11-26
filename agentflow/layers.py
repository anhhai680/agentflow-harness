"""The config layers: how the engine defaults, a starter, the project pack and the user file combine.

A leaf module: it imports nothing from `config.py`, which loads the layers through it. `base_layers` reads the
layers the engine owns (its defaults and a starter); the pack and the user file are read by `config.py`. `merge`
only applies the layers it is given, bottom-up. `check_user` is the trust boundary for the user's own file, which
may hold personal settings (`USER_KEYS`) and nothing else: no command, no guardrail, no gate, no new agent.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, NamedTuple

import yaml

ENGINE_DEFAULTS = Path(__file__).resolve().parent / "defaults" / "config.yaml"
STARTERS = Path(__file__).resolve().parent / "starters"  # one directory per starter, each holding a `config.yaml`
WORKFLOW_KEYS = (
    "lanes", "phases", "conditions", "record_step", "resolve_step", "phase_approval", "checkpoints", "autonomy",
    "ci", "skills", "route",
)
PHASE_FREE_KEYS = ("needs_approval", "resolve", "model", "agent")  # the rest of a phase is the workflow itself
USER_KEYS = (  # dotted patterns of the settings that are personal; `*` matches exactly one segment
    "agents.*.models", "agents.claude.user_settings", "defaults.run_budget_usd", "defaults.run_budget_hard_usd",
    "defaults.task_budget_usd", "hitl.notify", "hitl.remind_after_h", "dashboard.port", "usage.*",
    "workspace.roots", "workspace.repos", "workspace.exclude", "dashboard.board_refresh_s", "dashboard.done_shown",
)
MAX_WORKSPACE_ENTRIES = 256  # per list of `workspace`: a hand-written file never nears it, a hostile one is cut off
WORKSPACE_KEYS = ("roots", "repos", "exclude")
_WHOLE_ENTRIES = (("record_step",), ("resolve_step",))  # replaced whole, like each entry of `conditions`/`gates`
_WHOLE_PARENTS = ("conditions", "gates", "policy.obligations")  # dotted: each entry below one is replaced whole
_WHOLE_PARENT_KEYS = tuple(tuple(dotted.split(".")) for dotted in _WHOLE_PARENTS)
_PHASES_MESSAGE = "a phase's steps, and the set of phases, are changed by writing the whole workflow"
_NOT_PERSONAL = "not a personal setting; set it in the pack"

Key = tuple[str, ...]


class Layer(NamedTuple):
    name: str  # "engine", "project", "user" or "starter:<name>"
    data: dict
    path: Path | None


class LayerError(ValueError):
    """A layer that breaks a rule of the stack; `path` is the dotted key (the user layer's carries its file)."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


def user_config_path() -> Path:
    """`$XDG_CONFIG_HOME/agentflow/config.yaml` (when the variable is an absolute path), else `~/.config/...`."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / ".config"
    return base / "agentflow" / "config.yaml"


def starter_names() -> list[str]:
    """The names of the starters the engine ships: the directories of `STARTERS`, sorted (not a hidden one, and not
    a symlink, which could lead out of the folder)."""
    try:
        return sorted(p.name for p in STARTERS.iterdir()
                      if not p.name.startswith(".") and not p.is_symlink() and p.is_dir()
                      and (p / "config.yaml").is_file())
    except OSError:
        return []


def starter_file(starter: object) -> Path | None:
    """The config file of the starter a pack names; None when it names none (the key is absent or null).

    A name is looked up in `starter_names()` and the path is built from the listed entry, never from the value the
    pack wrote, so a value that is a path (`../defaults`, `/etc`), empty or not text can reach nothing outside
    `STARTERS`. Anything that is not a listed name is a `LayerError` at `starter`."""
    if starter is None:
        return None
    names = starter_names()
    if isinstance(starter, str):
        for listed in names:
            if listed == starter:
                return STARTERS / listed / "config.yaml"
    raise LayerError("starter", f"unknown starter {starter!r}; the starters are {', '.join(names)}")


def base_layers(starter: object) -> list[Layer]:
    """The layers under the pack: the engine defaults, then the starter the pack names (`starter_file`)."""
    stack = [Layer("engine", _read(ENGINE_DEFAULTS), ENGINE_DEFAULTS)]
    path = starter_file(starter)
    if path is not None:
        stack.append(Layer(f"starter:{path.parent.name}", _read(path), path))
    return stack


MAX_DEPTH = 64  # a config file nested deeper than this is refused: no config needs it, and every pass recurses
MAX_EXPANDED_NODES = 100_000  # a file whose aliases expand past this many values is refused (the shipped ones hold < 1000)
MAX_CONFIG_BYTES = 128 * 1024  # a config file longer than this is not parsed (the shipped ones are under 32 KiB)


class ConfigRefused(ValueError):
    """A config file `parse_config` refuses; its text completes a sentence that starts with the file's name."""


def _measure(node: Any, depth: int, memo: dict[int, tuple[int, int]], open_: set[int]) -> tuple[int, int]:
    """`(size, height)` of `node` with every alias copied out: its value count (a mapping's keys are not counted) and
    how many container levels lie below it. A shared node is walked once and its result reused at every use, and its
    height is checked again at the depth of each use, so nine anchored lists of nine references each cost 81 steps,
    and a chain of shallow anchors is measured at the depth it really reaches. The recursion never goes deeper than
    `MAX_DEPTH + 1` containers."""
    if not isinstance(node, (dict, list)):
        return 1, 0
    key = id(node)
    if key in open_:
        raise ConfigRefused("holds a YAML anchor that refers to itself")
    if key not in memo:
        if depth > MAX_DEPTH:
            raise ConfigRefused(f"is nested more than {MAX_DEPTH} levels deep")
        open_.add(key)
        size, height = 1, 0
        for child in node.values() if isinstance(node, dict) else node:
            child_size, child_height = _measure(child, depth + 1, memo, open_)
            size += child_size
            height = max(height, child_height + 1)
        open_.discard(key)
        memo[key] = (size, height)
    size, height = memo[key]
    if depth + height > MAX_DEPTH:
        raise ConfigRefused(f"is nested more than {MAX_DEPTH} levels deep")
    return size, height


def expanded_size(node: Any) -> int:
    """How many values `node` holds once every YAML alias is copied out: 1 for a scalar, 1 plus its children's sizes
    for a mapping (its values; keys are not counted) or a list. Raises `ValueError` for a value that contains itself
    or nests past `MAX_DEPTH` (`expansion_problem` says which)."""
    return _measure(node, 0, {}, set())[0]


def expansion_problem(node: Any) -> str | None:
    """Why a parsed config file is refused, as the end of a sentence that starts with the file's name, or None: it
    holds a value that contains itself (an anchor used inside its own node), nests past `MAX_DEPTH` (counted through
    every alias), or expands past `MAX_EXPANDED_NODES` values. Every later pass copies aliases out and recurses, so
    this is checked on each config file right after it is parsed."""
    try:
        size, _ = _measure(node, 0, {}, set())
    except ConfigRefused as refused:
        return str(refused)
    if size > MAX_EXPANDED_NODES:
        return f"expands to more than {MAX_EXPANDED_NODES} values (nested aliases)"
    return None


_MERGE_TAG = "tag:yaml.org,2002:merge"


def _merged_entries(root: yaml.Node) -> int:
    """An upper bound on the mapping entries PyYAML's constructor builds for the composed document `root`: each
    mapping's own entries plus, for each `<<` merge key, the entries of every mapping it merges (after their own
    merges, counted again for each use), summed over every mapping node once. One pass over the node graph, with no
    recursion; the count stops just past `MAX_EXPANDED_NODES`. A merge that reaches its own mapping is refused."""
    cap = MAX_EXPANDED_NODES + 1
    mappings: list[yaml.MappingNode] = []
    seen: set[int] = set()
    stack: list[yaml.Node] = [root]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, yaml.MappingNode):
            mappings.append(node)
            for key, value in node.value:
                stack.extend((key, value))
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(node.value)

    def sources(node: yaml.MappingNode) -> list[yaml.MappingNode]:
        found = []
        for key, value in node.value:
            if key.tag == _MERGE_TAG:
                merged = value.value if isinstance(value, yaml.SequenceNode) else [value]
                found.extend(source for source in merged if isinstance(source, yaml.MappingNode))
        return found

    sizes: dict[int, int] = {}
    total = 0
    for start in mappings:
        work: list[tuple[yaml.MappingNode, bool]] = [(start, False)]
        open_: set[int] = set()
        while work:
            node, finished = work.pop()
            key = id(node)
            if key in sizes:
                continue
            if finished:
                own = sum(1 for k, _ in node.value if k.tag != _MERGE_TAG)
                sizes[key] = min(cap, own + sum(sizes[id(source)] for source in sources(node)))
                open_.discard(key)
                continue
            if key in open_:  # popped again before it finished: a merge it holds leads back to it
                raise ConfigRefused("holds a YAML merge key (<<) that merges itself")
            open_.add(key)
            work.append((node, True))
            work.extend((source, False) for source in sources(node) if id(source) not in sizes)
        total = min(cap, total + sizes[id(start)])
    return total


def parse_config(text: str) -> Any:
    """`text` parsed as one YAML document, for a config file. It is composed first (one pass) and refused with
    `ConfigRefused` before it is built when its merge keys would build more than `MAX_EXPANDED_NODES` entries (a
    chain of `<<` costs the square of its length to build), and after it is built when it has an
    `expansion_problem`. Raises `yaml.YAMLError` for text that is not YAML."""
    loader = yaml.SafeLoader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            return None
        if _merged_entries(node) > MAX_EXPANDED_NODES:
            raise ConfigRefused(f"expands to more than {MAX_EXPANDED_NODES} values (merge keys)")
        data = loader.construct_document(node)
    finally:
        loader.dispose()
    problem = expansion_problem(data)
    if problem:
        raise ConfigRefused(problem)
    return data


def read_config_text(path: Path) -> str | None:
    """The text of the config file at `path`, or None when it is longer than `MAX_CONFIG_BYTES` (it is never parsed). Raises `OSError` as opening it does and
    `UnicodeDecodeError` for text that is not UTF-8."""
    with path.open("rb") as handle:
        data = handle.read(MAX_CONFIG_BYTES + 1)
    if len(data) > MAX_CONFIG_BYTES:
        return None
    return data.decode("utf-8")


def _read(path: Path, what: str = "layer", *, empty_ok: bool = False) -> dict:
    """A layer file's mapping. A file that cannot be read, is longer than `MAX_CONFIG_BYTES`, is not YAML, is refused
    by `parse_config`, or is not a mapping at the top level (an empty one is `{}` when `empty_ok`) is a `LayerError`."""
    try:
        text = read_config_text(path)
        if text is None:
            raise LayerError(str(path), f"is larger than {MAX_CONFIG_BYTES} bytes")
        loaded = parse_config(text)
    except ConfigRefused as refused:
        raise LayerError(str(path), str(refused)) from refused
    except (OSError, yaml.YAMLError) as exc:
        raise LayerError(str(path), f"cannot read the {what} file: {exc}") from exc
    except RecursionError as exc:
        raise LayerError(str(path), f"the {what} file is nested too deeply to read") from exc
    if loaded is None and empty_ok:
        return {}
    if not isinstance(loaded, dict):
        raise LayerError(str(path), f"a {what} file must hold a mapping at the top level")
    return loaded


def owns_workflow(data: dict) -> bool:
    """True when `phases` maps at least one phase to a mapping that holds `steps`: the layer writes the workflow."""
    phases = data.get("phases")
    return isinstance(phases, dict) and any(isinstance(p, dict) and "steps" in p for p in phases.values())


def _is_whole(key: Key) -> bool:
    return key in _WHOLE_ENTRIES or key[:-1] in _WHOLE_PARENT_KEYS


def _drop_origins(origins: dict[Key, str], prefix: Key) -> None:
    for key in [k for k in origins if k[: len(prefix)] == prefix]:
        del origins[key]


def _apply(into: dict, patch: dict, name: str, origins: dict[Key, str], prefix: Key) -> None:
    """Merge `patch` into `into` (both mappings) in place; `into` and `origins` are the merge's own copies."""
    for key, value in patch.items():
        here = (*prefix, key)
        if value is None:  # null deletes the key
            into.pop(key, None)
            _drop_origins(origins, here)
        elif isinstance(value, dict) and value and not _is_whole(here):
            if not isinstance(into.get(key), dict):
                into[key] = {}
                _drop_origins(origins, here)
            _apply(into[key], value, name, origins, here)
        elif isinstance(value, dict) and not value and isinstance(into.get(key), dict) and not _is_whole(here):
            continue  # merging nothing into a mapping changes nothing
        else:  # a scalar, a list, a mismatched type, an empty mapping with nothing below it, or a whole entry
            into[key] = copy.deepcopy(value)
            _drop_origins(origins, here)
            origins[here] = name


def _check_phases(layer: Layer, merged: dict) -> None:
    phases = layer.data["phases"]
    if not isinstance(phases, dict):
        raise LayerError("phases", f"phases must be a mapping of phase knobs; {_PHASES_MESSAGE}")
    below = merged.get("phases")
    for name, knobs in phases.items():
        where = f"phases.{name}"
        if not isinstance(knobs, dict):
            raise LayerError(where, f"a phase must be a mapping of {', '.join(PHASE_FREE_KEYS)}; {_PHASES_MESSAGE}")
        if not isinstance(below, dict) or not isinstance(below.get(name), dict):
            raise LayerError(where, f"no phase {name!r} below this layer; {_PHASES_MESSAGE}")
        for key in knobs:
            if key not in PHASE_FREE_KEYS:
                raise LayerError(f"{where}.{key}", f"only {', '.join(PHASE_FREE_KEYS)} can be set; {_PHASES_MESSAGE}")


def merge(stack: list[Layer]) -> tuple[dict, dict[Key, str]]:
    """Apply `stack` bottom-up. Returns the merged data and, for each leaf, the name of the layer it came from.

    Mappings merge key by key. Lists, scalars and mismatched types are replaced. Each entry of `conditions.*` and
    `gates.*`, `record_step` and `resolve_step` are replaced whole. `null` deletes a key. The inputs are not
    mutated and nothing in the result is shared with them. A leaf is a scalar, a list, an empty mapping, or a whole
    entry (`gates.<name>`, `conditions.<name>`, `record_step`, `resolve_step`).
    """
    merged: dict = {}
    origins: dict[Key, str] = {}
    for layer in stack:
        if owns_workflow(layer.data):
            if "lanes" not in layer.data:
                raise LayerError("lanes", "a pack that writes its own phases also writes `lanes`")
            for key in WORKFLOW_KEYS:
                merged.pop(key, None)
                _drop_origins(origins, (key,))
        elif "phases" in layer.data:
            _check_phases(layer, merged)
        _apply(merged, layer.data, layer.name, origins, ())
    return merged, origins


def leaf_keys(data: dict) -> list[Key]:
    """The leaves `merge` records for `data` as the only layer: what a pack file states (a null states nothing)."""
    out: list[Key] = []

    def walk(node: dict, prefix: Key) -> None:
        for key, value in node.items():
            here = (*prefix, key)
            if value is None:
                continue
            if isinstance(value, dict) and value and not _is_whole(here):
                walk(value, here)
            else:
                out.append(here)

    walk(data, ())
    return out


def rows(merged: dict, origins: dict[Key, str]) -> list[tuple[str, Any, str]]:
    """One `(dotted key, value, layer)` per leaf of `merged` (as `leaf_keys` walks it), in merged order. A leaf the
    origins don't name itself, such as a key set below a lower layer's `{}`, takes the layer `origin_of` finds."""
    out: list[tuple[str, Any, str]] = []
    for key in leaf_keys(merged):
        value: Any = merged
        for part in key:
            value = value[part]
        dotted = ".".join(str(part) for part in key)
        out.append((dotted, value, origins.get(key) or origin_of(origins, dotted) or "?"))
    return out


def _leaves(data: Any, prefix: Key = ()) -> list[Key]:
    if isinstance(data, dict) and data:
        return [leaf for key, value in data.items() for leaf in _leaves(value, (*prefix, str(key)))]
    return [prefix] if prefix else []  # an empty file has no leaf


def _text_keys(data: Any, path: Path, prefix: Key = ()) -> None:
    """Refuse a key that is not text anywhere in `data`: `merge` uses a key as it is, so an int `1` must never pass
    the checks below as the text `"1"` (an agent the pack defines) and then create an agent of its own."""
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        if not isinstance(key, str):
            where = ".".join((*prefix, str(key)))
            raise LayerError(f"{path}: {where}", "a key in a user config file is text: quote it")
        _text_keys(value, path, (*prefix, key))


def check_user(data: dict, lower: dict, path: Path, *, any_agent: bool = False) -> None:
    """Refuse a user file that holds anything but `USER_KEYS`, or names an agent the layers below don't define
    (`any_agent` skips that check: a caller with no pack cannot know the agents a pack defines)."""
    if not isinstance(data, dict):
        raise LayerError(str(path), "a user config file is a mapping of personal settings")
    _text_keys(data, path)
    for leaf in _leaves(data):
        dotted = ".".join(leaf)
        where = f"{path}: {dotted}"
        for pattern in USER_KEYS:
            segments = pattern.split(".")
            if len(leaf) >= len(segments) and all(s in ("*", k) for s, k in zip(segments, leaf)):
                break
        else:
            raise LayerError(where, _NOT_PERSONAL)
        node: Any = lower
        for segment, key in zip(segments, leaf):
            if segment == "*" and not any_agent and not (isinstance(node, dict) and key in node):
                raise LayerError(where, f"no agent `{key}` in the pack")
            node = node.get(key) if isinstance(node, dict) else None


def origin_of(origins: dict[Key, str], dotted: str) -> str | None:
    """The origin of the longest leaf whose key is `dotted` or a prefix of it, to say which file an error is from."""
    parts = tuple(dotted.split("."))
    best: Key | None = None
    for key in origins:
        if parts[: len(key)] == key and (best is None or len(key) > len(best)):
            best = key
    return origins[best] if best is not None else None


class WorkspaceSettings(NamedTuple):
    roots: tuple[str, ...]  # folders whose child repositories belong to the workspace
    repos: tuple[str, ...]  # single repositories
    exclude: tuple[str, ...]  # a path, or a bare folder name, to leave out


class UserSettings(NamedTuple):
    workspace: WorkspaceSettings
    dashboard: dict  # the merged `dashboard` block: host, port, and board_refresh_s / done_shown when set
    path: Path | None  # the user file, when it exists


def _path_shaped(entry: str) -> bool:
    """`/...`, `~` or `~/...`: never relative and never `~user`."""
    return entry.startswith("/") or entry == "~" or entry.startswith("~/")


def workspace_settings(block: object) -> WorkspaceSettings:
    """The `workspace` block as three tuples of entries; None (no block) is all empty.

    Every entry is checked here once, so a reader can take the shape as given: text that is not empty, at most
    `MAX_WORKSPACE_ENTRIES` per list. A `roots` or `repos` entry, and an `exclude` entry that holds a `/` or starts
    with `~`, is an absolute path or one under the home directory (`~`, `~/...`); an `exclude` entry with neither is
    a bare folder name. A `LayerError` names `workspace.<key>`."""
    if block is None:
        return WorkspaceSettings((), (), ())
    if not isinstance(block, dict):
        raise LayerError("workspace", "must be a mapping of roots, repos and exclude")
    for key in block:
        if key not in WORKSPACE_KEYS:
            raise LayerError(f"workspace.{key}", f"unknown key; known: {', '.join(WORKSPACE_KEYS)}")
    lists: dict[str, tuple[str, ...]] = {}
    for key in WORKSPACE_KEYS:
        where = f"workspace.{key}"
        value = block.get(key)
        if value is None:
            lists[key] = ()
            continue
        if not isinstance(value, list):
            raise LayerError(where, "must be a list of paths")
        if len(value) > MAX_WORKSPACE_ENTRIES:
            raise LayerError(where, f"holds {len(value)} entries; at most {MAX_WORKSPACE_ENTRIES}")
        for entry in value:
            if not isinstance(entry, str) or not entry:
                raise LayerError(where, "every entry is non-empty text")
            if not entry.isprintable():
                raise LayerError(where, f"{entry!r} holds a control character")
            if (key != "exclude" or "/" in entry or entry.startswith("~")) and not _path_shaped(entry):
                raise LayerError(where, f"{entry!r} must be an absolute path or start with ~/ (never `~user` or relative)")
        lists[key] = tuple(value)
    return WorkspaceSettings(lists["roots"], lists["repos"], lists["exclude"])


def load_user_settings() -> UserSettings:
    """The user's personal settings alone: the engine defaults plus the user file, with no pack.

    The file is checked (`check_user`) against the engine layer and its `workspace` block is shaped by
    `workspace_settings`; a `LayerError` for either names the file. No home directory, or no file, means the engine
    defaults and `path` None."""
    stack = base_layers(None)
    merged, _ = merge(stack)
    path: Path | None = None
    found = False
    try:
        candidate = user_config_path()
    except RuntimeError:  # no home directory to look in: there is no user file
        pass
    else:
        try:
            found = candidate.exists()
        except OSError as exc:  # an unreadable parent directory: say which file
            raise LayerError(str(candidate), f"cannot read the user config file: {exc}") from exc
    if found:
        path = candidate
        data = _read(path, "user config", empty_ok=True)
        check_user(data, merged, path, any_agent=True)
        merged, _ = merge([*stack, Layer("user", data, path)])
    try:
        workspace = workspace_settings(merged.get("workspace"))
    except LayerError as exc:
        raise LayerError(f"{path}: {exc.path}" if path else exc.path, exc.message) from exc
    dashboard = merged.get("dashboard")
    return UserSettings(workspace, copy.deepcopy(dashboard) if isinstance(dashboard, dict) else {}, path)
