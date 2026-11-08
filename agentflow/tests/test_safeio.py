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


def _in_time(fn, seconds: float = 5.0):
    """Run `fn` in a thread and return (finished, result-or-exception); a FIFO open that blocks never finishes."""
    import threading

    box: dict = {}

    def go():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - the test inspects it
            box["value"] = exc

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    thread.join(seconds)
    return not thread.is_alive(), box.get("value")


def test_reading_a_fifo_never_blocks(tmp_path: Path):
    fifo = tmp_path / "lessons.md"
    os.mkfifo(fifo)
    done, value = _in_time(lambda: safeio.read_bytes(fifo, 1000))
    if not done:  # open a writer so the stuck open returns and the thread ends
        os.close(os.open(fifo, os.O_WRONLY))
    assert done, "read_bytes blocked on a FIFO"
    assert isinstance(value, ValueError) and "regular file" in str(value)


def test_writing_a_fifo_never_blocks_and_never_writes_into_it(tmp_path: Path):
    fifo = tmp_path / "lessons.md"
    os.mkfifo(fifo)
    done, value = _in_time(lambda: safeio.write_text(fifo, "text"))
    if not done:
        os.close(os.open(fifo, os.O_RDONLY | os.O_NONBLOCK))
    assert done, "write_text blocked on a FIFO"
    assert isinstance(value, OSError | ValueError)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)  # with a reader present the open succeeds: still refused
    try:
        done, value = _in_time(lambda: safeio.write_text(fifo, "text"))
        assert done and isinstance(value, ValueError) and "regular file" in str(value)
        assert os.read(reader, 100) == b""
    finally:
        os.close(reader)
