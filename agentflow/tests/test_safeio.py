"""`safeio.write_new`: create a file exclusively, never through a symlink and never over anything already there."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from agentflow import safeio


def test_write_new_creates_the_file_with_its_mode(tmp_path: Path):
    target = tmp_path / "dir" / "config.yaml"
    assert safeio.write_new(target, "a: 1\n") == target
    assert target.read_text(encoding="utf-8") == "a: 1\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644 & ~_umask()
    other = tmp_path / "private.txt"
    safeio.write_new(other, "x", mode=0o600)
    assert stat.S_IMODE(other.stat().st_mode) == 0o600 & ~_umask()


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def test_write_new_refuses_an_existing_file_and_a_symlink(tmp_path: Path):
    existing = tmp_path / "existing.yaml"
    existing.write_text("mine\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        safeio.write_new(existing, "theirs\n")
    assert existing.read_text(encoding="utf-8") == "mine\n"

    victim = tmp_path / "victim.txt"
    victim.write_text("untouched\n", encoding="utf-8")
    link = tmp_path / "link.yaml"
    link.symlink_to(victim)
    with pytest.raises(FileExistsError):
        safeio.write_new(link, "theirs\n")
    assert victim.read_text(encoding="utf-8") == "untouched\n"

    dangling = tmp_path / "dangling.yaml"
    dangling.symlink_to(tmp_path / "nowhere.txt")
    with pytest.raises(FileExistsError):
        safeio.write_new(dangling, "theirs\n")
    assert not (tmp_path / "nowhere.txt").exists()


def test_write_new_refuses_a_symlinked_parent(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "linked").symlink_to(real, target_is_directory=True)
    with pytest.raises(safeio.SymlinkRefused):
        safeio.write_new(tmp_path / "linked" / "config.yaml", "x\n")
    assert list(real.iterdir()) == []
