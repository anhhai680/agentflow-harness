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
    "ci", "skills",
)
PHASE_FREE_KEYS = ("needs_approval", "resolve", "model", "agent")  # the rest of a phase is the workflow itself
USER_KEYS = (  # dotted patterns of the settings that are personal; `*` matches exactly one segment
    "agents.*.models", "defaults.run_budget_usd", "defaults.task_budget_usd", "hitl.notify", "hitl.remind_after_h",
    "dashboard.port",
)
_WHOLE_ENTRIES = (("record_step",), ("resolve_step",))  # replaced whole, like each entry of `conditions`/`gates`
_WHOLE_PARENTS = ("conditions", "gates")
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


def _read(path: Path) -> dict:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LayerError(str(path), f"cannot read the layer file: {exc}") from exc
    if not isinstance(loaded, dict):
        raise LayerError(str(path), "a layer file must hold a mapping at the top level")
    return loaded


def owns_workflow(data: dict) -> bool:
    """True when `phases` maps at least one phase to a mapping that holds `steps`: the layer writes the workflow."""
    phases = data.get("phases")
    return isinstance(phases, dict) and any(isinstance(p, dict) and "steps" in p for p in phases.values())


def _is_whole(key: Key) -> bool:
    return key in _WHOLE_ENTRIES or (len(key) == 2 and key[0] in _WHOLE_PARENTS)


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


def check_user(data: dict, lower: dict, path: Path) -> None:
    """Refuse a user file that holds anything but `USER_KEYS`, or names an agent the layers below don't define."""
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
            if segment == "*" and not (isinstance(node, dict) and key in node):
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
