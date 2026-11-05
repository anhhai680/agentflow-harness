"""Which config a run may continue with when the command line names none.

A config decides which shell commands the runner runs (gates, `test_command` checks), so a config path
taken from a run's own record is loaded only when one of these holds; otherwise the run is refused with
a message that says how to go on (`--config <path>`):

1. it is this checkout's shipped config (`devtools/agentflow/config.yaml`), or
2. it is the config the run was started with (the run's first `run_options` event, written by `start`),
   and the file's SHA-256 still equals the hash recorded then.

The run's later `run_options` events cannot point it at another file: a different path there is refused.
An explicit `--config` on the invocation always wins (the human named it), and is not checked here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from agentflow.config import ConfigError
from agentflow.state import Run

__all__ = ["ConfigUntrusted", "config_sha256", "start_options", "trusted_config_path"]


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
    docstring), else the shipped `default` when nothing is recorded. Raises `ConfigUntrusted`."""
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
                              "Pass `--config <path>` to choose one.") from exc
    if not recorded or current != recorded:
        raise ConfigUntrusted(started, f"{started} changed since {run.id} started (its SHA-256 is not the one "
                              f"recorded then); refusing to load it. Pass `--config {started}` to use it as it is now.")
    return path
