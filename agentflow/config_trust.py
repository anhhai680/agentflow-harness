"""Which config a run may continue with when the command line names none.

A config decides which shell commands the runner runs (gates, `test_command` checks), so a config path
taken from a run's own record is loaded only when one of these holds; otherwise the run is refused with
a message that says how to go on (`--config <path>`):

1. it is the repository's pack (`<main checkout>/.agentflow/config.yaml`, the `default` below), or
2. it is the config the run was started with (the run's first `run_options` event, written by `start`),
   and the file's SHA-256 still equals the hash recorded then.

The run's later `run_options` events cannot point it at another file: a different path there is refused.
An explicit `--config` on the invocation always wins (the human named it), and is not checked here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from agentflow import pack
from agentflow.config import Config, ConfigError, layer_files
from agentflow.state import Run

__all__ = ["ConfigUntrusted", "changed_layers", "config_sha256", "layers_sha256", "run_config_path", "start_options",
           "trusted_config_path"]


class ConfigUntrusted(ConfigError):
    """The config recorded for a run is not one the runner may load without an explicit `--config`."""


def config_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def layers_sha256(paths: list[Path]) -> str:
    """One SHA-256 over each file's path and bytes, in order: it changes when a layer file's content, its place in
    the stack or the set of files changes. Bytes only: a broken file still has a digest."""
    digest = hashlib.sha256()
    for path in paths:
        path = Path(path)
        digest.update(str(path).encode("utf-8", "surrogateescape") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def start_options(path: Path, layer_paths: list[Path] | None = None) -> dict[str, Any]:
    """What `start` records in `run_options` about its config: the absolute path and the content hash, and with
    `layer_paths` (`config.layer_files`) the digest of all the layers and each file's own digest, so a resume can
    name the layer files that changed since."""
    path = Path(path).resolve()
    options: dict[str, Any] = {"config": str(path), "config_sha256": config_sha256(path)}
    if layer_paths is not None:
        options["layers_sha256"] = layers_sha256(layer_paths)
        options["layer_digests"] = {str(p): config_sha256(p) for p in layer_paths}
    return options


def changed_layers(run: Run, cfg: Config) -> list[Path]:
    """The layer files of `cfg` that differ from the ones `run` started with: changed, new or gone. Empty when
    nothing changed, when the run was started before the digests were recorded, or when `cfg` is not the config the
    run started with (a `--config` the human named). It never decides what is trusted: see `trusted_config_path`."""
    first = next((e for e in run.events.read() if e["type"] == "run_options"), None)
    recorded = (first or {}).get("layer_digests")
    if not isinstance(recorded, dict) or not recorded:
        return []
    if str((first or {}).get("config") or "") != str(Path(cfg.path).resolve()):
        return []
    try:
        now = {str(p): config_sha256(p) for p in layer_files(cfg)}
    except OSError:
        return []  # a layer that cannot be read now is a load error the run reports itself
    names = [name for name in recorded if recorded.get(name) != now.get(name)]
    names += [name for name in now if name not in recorded]
    return [Path(name) for name in names]


def trusted_config_path(run: Run, explicit: str | Path | None, default: Path) -> Path:
    """The config `run` continues with: `explicit`, else the recorded one if trusted (see the module
    docstring), else `default` (the repository's pack) when nothing is recorded. Raises `ConfigUntrusted`."""
    if explicit:
        return Path(explicit)
    default = Path(default).resolve()
    first = next((e for e in run.events.read() if e["type"] == "run_options"), None)
    started = str((first or {}).get("config") or "")
    latest = str((run.state.get("options") or {}).get("config") or "")
    if latest and latest != started:
        raise ConfigUntrusted(latest, f"{run.id} records a config it was not started with ({started or 'none'}); "
                              f"refusing to load it. Pass `--config <path>` to choose one.")
    if not started:
        return default
    path = Path(started).resolve()
    if path == default:
        return path
    recorded = str(first.get("config_sha256") or "") if first else ""
    try:
        current = config_sha256(path)
    except OSError as exc:
        raise ConfigUntrusted(started, f"{run.id} was started with {started}, which cannot be read ({exc}). "
                              "Pass `--config <path>` to choose one. "
                              f"Pass --config {pack.PACK_DIR}/config.yaml to use the repository's pack.") from exc
    if not recorded or current != recorded:
        raise ConfigUntrusted(started, f"{started} changed since {run.id} started (its SHA-256 is not the one "
                              f"recorded then); refusing to load it. Pass `--config {started}` to use it as it is now.")
    return path


def run_config_path(run: Run, explicit: str | Path | None = None) -> Path:
    """`trusted_config_path` with the repository's pack as the default. A repository without a pack still
    continues a run on the trusted config it records; `PackError` only when the pack is the one it would load."""
    if explicit:
        return Path(explicit)
    default = pack.pack_config_path(run.repo)
    path = trusted_config_path(run, None, default)
    if path == default.resolve():
        return pack.pack_config(run.repo)  # `PackError` when the repository has no pack
    return path
