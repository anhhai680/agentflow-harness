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

from agentflow import pack
from agentflow.config import ConfigError
from agentflow.state import Run

__all__ = ["ConfigUntrusted", "config_sha256", "run_config_path", "start_options", "trusted_config_path"]


class ConfigUntrusted(ConfigError):
    """The config recorded for a run is not one the runner may load without an explicit `--config`."""


def config_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def start_options(path: Path) -> dict[str, str]:
    """What `start` records in `run_options` about its config: the absolute path and the content hash."""
    path = Path(path).resolve()
    return {"config": str(path), "config_sha256": config_sha256(path)}


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
