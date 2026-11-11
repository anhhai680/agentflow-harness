from __future__ import annotations

import subprocess
import sys

import pytest

from agentflow import keepawake

REAL_ARGV = keepawake.inhibitor_argv  # conftest stubs it for every other test


@pytest.fixture(autouse=True)
def _real_lookup(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(keepawake, "inhibitor_argv", REAL_ARGV)


@pytest.fixture
def which(monkeypatch: pytest.MonkeyPatch):
    found: dict[str, str] = {}
    monkeypatch.setattr(keepawake.shutil, "which", lambda name: found.get(name))
    return found


def test_macos_uses_caffeinate_bound_to_the_runner(which):
    which["caffeinate"] = "/usr/bin/caffeinate"
    assert keepawake.inhibitor_argv(42, "darwin") == ["/usr/bin/caffeinate", "-i", "-w", "42"]


def test_linux_uses_systemd_inhibit_around_a_tail_of_the_runner(which):
    which.update({"systemd-inhibit": "/usr/bin/systemd-inhibit", "tail": "/usr/bin/tail"})
    argv = keepawake.inhibitor_argv(42, "linux")
    assert argv[0] == "/usr/bin/systemd-inhibit" and "--what=idle:sleep" in argv and "--mode=block" in argv
    assert argv[-4:] == ["/usr/bin/tail", "--pid=42", "-f", "/dev/null"]


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_no_program_means_no_inhibitor(which, platform):
    assert keepawake.inhibitor_argv(42, platform) is None
    assert keepawake.describe(platform) is None


def test_holding_starts_the_inhibitor_and_ends_it_with_the_block(which, monkeypatch):
    monkeypatch.delenv(keepawake.OFF_ENV, raising=False)
    started: list[subprocess.Popen] = []
    real = subprocess.Popen

    def popen(argv, **kw):
        proc = real([sys.executable, "-c", "import time; time.sleep(30)"], **kw)
        started.append(proc)
        return proc

    monkeypatch.setattr(keepawake, "inhibitor_argv", lambda pid, platform=None: ["inhibit", str(pid)])
    monkeypatch.setattr(keepawake.subprocess, "Popen", popen)
    with keepawake.holding():
        assert len(started) == 1 and started[0].poll() is None
    assert started[0].poll() is not None  # gone once the runner stops


def test_holding_never_fails_the_run(monkeypatch):
    monkeypatch.delenv(keepawake.OFF_ENV, raising=False)
    monkeypatch.setattr(keepawake, "inhibitor_argv", lambda pid, platform=None: ["/no/such/inhibitor"])
    with keepawake.holding():
        pass


def test_the_switch_turns_it_off(monkeypatch):
    monkeypatch.setenv(keepawake.OFF_ENV, "1")
    monkeypatch.setattr(keepawake, "inhibitor_argv", lambda pid, platform=None: pytest.fail("must not start"))
    with keepawake.holding():
        pass
