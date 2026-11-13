"""Pack prompt overrides that fell behind the engine, and the fixes `agentflow validate --fix` applies to them.

A pack overrides an engine prompt by putting a file of the same name in `.agentflow/prompts/`, usually a copy of
the engine's file with a few lines changed. When a release drops or rewords text in that engine prompt, the copy
keeps the old text. `RETIRED` lists every such piece of text, with the exact wording the engine shipped before:
`findings` warns about each override that still holds it, and fixes the ones that hold it unchanged. It also warns
about an override identical to the engine's file, which changes nothing and hides later engine fixes to that file.

A release that drops or rewords text in an engine prompt adds a `Retired` entry here in the same change.
"""
from __future__ import annotations

import difflib
import errno
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from agentflow import pack
from agentflow.config import Issue

MAX_PROMPT_BYTES = 1_000_000  # a prompt larger than this is skipped, never read whole


@dataclass(frozen=True)
class Retired:
    """One piece of engine prompt text a release dropped (`new == ""`) or replaced with `new`."""
    id: str  # stable: named in the warning
    file: str  # the engine prompt it came from
    retired_in: str  # the engine version that dropped it
    detect: str  # a token that marks the text even after a team edited it
    old: str  # the exact text the engine shipped before `retired_in`
    new: str  # what replaces `old`


RETIRED: tuple[Retired, ...] = (
    Retired(
        id="effort-check-block",
        file="_preamble.md",
        retired_in="2.5",
        detect="<!-- effort-check -->",
        old=(
            "<!-- effort-check -->\n"
            "## Effort check\n"
            "\n"
            "Before any work, size this step. Read its inputs (the skill, the spec section, the plan task and the "
            "files they name) and decide whether the default effort level (medium) fits.\n"
            "If it clearly does not (a one-line config tweak needs only `low`; a change across many files with hard "
            "edge cases needs `high`), stop now, change nothing, and write the result file with "
            "`\"status\": \"effort_change\"`, the level you want as `\"effort\"` and why as `\"reason\"`. The runner "
            "starts the step again at that level.\n"
            "Otherwise carry on at this level. You will not be asked again.\n"
            "<!-- /effort-check -->\n"
            "\n"
        ),
        new="",
    ),
    Retired(
        id="effort-change-status",
        file="_result.md",
        retired_in="2.5",
        detect="effort_change",
        old=(
            "- `status`: `done` (the step is finished), `blocked` (you cannot go on; say why in `summary`), "
            "`needs_human` (a decision the docs do not settle; add `questions`), `effort_change` (only from the "
            "effort check, with `effort` and `reason`), or `unchanged` (only in a refine round, when the note needs "
            "no change; give the reason in `summary`)."
        ),
        new=(
            "- `status`: `done` (the step is finished), `blocked` (you cannot go on; say why in `summary`), "
            "`needs_human` (a decision the docs do not settle; add `questions`), or `unchanged` (only in a refine "
            "round, when the note needs no change; give the reason in `summary`)."
        ),
    ),
)


@dataclass(frozen=True)
class Fix:
    """What `--fix` does to one pack prompt: write `after`, or delete the override when `after` is None."""
    path: Path
    before: bytes
    after: bytes | None


@dataclass(frozen=True)
class Skipped:
    path: Path
    reason: str


@dataclass
class Findings:
    issues: list[Issue] = field(default_factory=list)
    fixes: list[Fix] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)


_PROMPT_NAME = re.compile(r"[A-Za-z0-9_.-]+\.md")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def shown(text: str) -> str:
    """`text` safe to print to a terminal: every control character but tab and newline as `\\xNN`. File names and
    contents come from the repository, which may carry escape sequences."""
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", text)


def _read(path: Path, follow: bool) -> tuple[bytes, str] | Skipped:
    """The bytes and text of a prompt, read with one bounded open (following a symlink only when `follow`)."""
    flags = os.O_RDONLY | os.O_NONBLOCK | (0 if follow else os.O_NOFOLLOW)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        return Skipped(path, "a symlink" if exc.errno == errno.ELOOP or path.is_symlink() else "not readable")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return Skipped(path, "not a regular file")
        if info.st_size > MAX_PROMPT_BYTES:
            return Skipped(path, "larger than 1 MB")
        data = _read_fd(fd)
    finally:
        os.close(fd)
    if len(data) > MAX_PROMPT_BYTES:
        return Skipped(path, "larger than 1 MB")
    try:
        return data, data.decode("utf-8")
    except UnicodeDecodeError:
        return Skipped(path, "not UTF-8")


def _read_fd(fd: int) -> bytes:
    chunks, size = [], 0
    while size <= MAX_PROMPT_BYTES and (chunk := os.read(fd, 65536)):
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def _engine_bytes(name: str) -> bytes | None:
    file = pack.ENGINE_PROMPTS / name
    return file.read_bytes() if file.is_file() else None


def _not_in_place(path: Path) -> str | None:
    """Why `--fix` can't change this prompt where it is, or None when it can."""
    folder = path.parent
    if folder.is_symlink() or folder.parent.is_symlink():
        return "in a symlinked folder"
    if path.is_symlink():
        return "a symlink"
    if not _PROMPT_NAME.fullmatch(path.name):
        return "an unusual file name"
    return None


def _scan(text: str) -> tuple[str, list[Retired], list[Retired]]:
    """`text` with every exact retired text replaced, the entries that matched, and the entries whose token is
    still there after that (the text was edited)."""
    crlf = "\r\n" in text
    changed, matched = text, []
    for entry in RETIRED:
        old, new = (entry.old.replace("\n", "\r\n"), entry.new.replace("\n", "\r\n")) if crlf else (entry.old, entry.new)
        if old in changed:
            changed = changed.replace(old, new)
            matched.append(entry)
    return changed, matched, [entry for entry in RETIRED if entry.detect in changed]


def _edited(entry: Retired) -> str:
    return (f"mentions {entry.detect!r}, which the engine retired in {entry.retired_in} ({entry.id}), in text that "
            f"was edited, so validate --fix leaves it: compare it with engine:prompts/{entry.file}")


def findings(config_path: Path) -> Findings:
    """Every drift problem in the pack's own prompts (`<pack>/prompts/*.md`), with the fixes for those that have one
    right answer. Reads only. A prompt `--fix` can't change in place (a symlink, a symlinked folder, an unusual
    name) is still read, through its link, and its problems reported as not fixable."""
    out = Findings()
    folder = Path(config_path).parent / "prompts"
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.md")):
        label = pack.prompt_label(config_path, path)
        why_not = _not_in_place(path)
        read = _read(path, follow=why_not is not None)
        if isinstance(read, Skipped):
            out.skipped.append(Skipped(path, why_not or read.reason))
            if read.reason == "larger than 1 MB":
                out.issues.append(Issue("warning", label, "is larger than 1 MB, so validate can't check it for text "
                                                          "the engine retired"))
            continue
        before, text = read
        changed, matched, edited = _scan(text)
        if why_not:
            out.skipped.append(Skipped(path, why_not))
            for entry in matched:
                out.issues.append(Issue("warning", label, f"holds text the engine retired in {entry.retired_in} "
                                                          f"({entry.id}), but it is {why_not}, so validate --fix "
                                                          f"leaves it: compare it with engine:prompts/{entry.file}"))
            out.issues.extend(Issue("warning", label, _edited(entry)) for entry in edited)
            continue
        for entry in matched:
            does = "removes it" if not entry.new else "replaces it with the engine's current text"
            out.issues.append(Issue("warning", label, f"holds text the engine retired in {entry.retired_in} "
                                                      f"({entry.id}); validate --fix {does}", fixable=True))
        out.issues.extend(Issue("warning", label, _edited(entry)) for entry in edited)
        engine = _engine_bytes(path.name)
        if changed == text:
            if engine is not None and before == engine:
                out.issues.append(Issue("warning", label, f"is identical to engine:prompts/{path.name}: it changes "
                                                          f"nothing and hides later engine fixes to that file; "
                                                          f"validate --fix deletes it", fixable=True))
                out.fixes.append(Fix(path, before, None))
            continue
        after = changed.encode("utf-8")
        out.fixes.append(Fix(path, before, None if after == engine else after))
    return out


# ---- applying the fixes (`agentflow validate --fix`)


class FixRefused(Exception):
    """`validate --fix` will not touch this pack at all."""


def check_pack(repo: Path) -> None:
    """Refuse `--fix` inside an agentflow run (an agent never changes the pack; the variable counts even when empty),
    and on a pack whose folder or `prompts/` folder is a symlink (the writes would land outside the repository)."""
    if "AGENTFLOW_RUN" in os.environ:
        raise FixRefused("validate --fix never runs inside an agentflow run")
    for folder in (Path(repo) / pack.PACK_DIR, Path(repo) / pack.PACK_DIR / "prompts"):
        if folder.is_symlink():
            raise FixRefused(f"{folder} is a symlink")


def _rel(path: Path, repo: Path) -> str:
    return os.path.relpath(path, repo)


def render(fix: Fix, repo: Path) -> str:
    """What the fix does, as a unified diff with `a/` and `b/` headers, or `delete <path>`; escaped for a terminal."""
    rel = shown(_rel(fix.path, repo))
    if fix.after is None:
        return f"delete {rel}\n"
    lines = difflib.unified_diff(fix.before.decode("utf-8").splitlines(keepends=True),
                                 fix.after.decode("utf-8").splitlines(keepends=True),
                                 fromfile=f"a/{rel}", tofile=f"b/{rel}")
    return "".join(shown(line.removesuffix("\n").removesuffix("\r")) + "\n" for line in lines)


def _inside_prompts(path: Path) -> bool:
    folder = path.parent
    return (bool(_PROMPT_NAME.fullmatch(path.name)) and folder.name == "prompts" and folder.parent.name == pack.PACK_DIR
            and not folder.is_symlink() and not folder.parent.is_symlink())


_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _apply_one(fix: Fix) -> str | None:
    """Apply one fix through directory handles opened without following a symlink, so no folder on the way can be
    swapped for a link once checked. The file must still be the one regular, singly linked file `findings` read,
    holding the same bytes; a rewrite goes to a new file renamed over it, so nothing is written through a link and
    an interrupted write leaves the old prompt. Returns why it was skipped, or None once applied."""
    name = fix.path.name
    pack_dir = os.open(fix.path.parent.parent, _DIR)
    try:
        folder = os.open("prompts", _DIR, dir_fd=pack_dir)
    finally:
        os.close(pack_dir)
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        except OSError:
            return "changed since it was read"
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PROMPT_BYTES:
                return "changed since it was read"
            if info.st_nlink != 1:
                return "has other hard links"
            now = _read_fd(fd)
        finally:
            os.close(fd)
        if now != fix.before:
            return "changed since it was read"

        def same() -> bool:
            current = os.stat(name, dir_fd=folder, follow_symlinks=False)
            return (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino)

        if fix.after is None:
            if not same():
                return "changed since it was read"
            os.unlink(name, dir_fd=folder)
            return None
        temp = f".{name}.fix-{os.getpid()}"
        out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, stat.S_IMODE(info.st_mode),
                      dir_fd=folder)
        try:
            try:
                os.fchmod(out, stat.S_IMODE(info.st_mode))
                view = memoryview(fix.after)
                while view:
                    view = view[os.write(out, view):]
                os.fsync(out)
            finally:
                os.close(out)
            if not same():
                os.unlink(temp, dir_fd=folder)
                return "changed since it was read"
            os.replace(temp, name, src_dir_fd=folder, dst_dir_fd=folder)
        except BaseException:
            try:
                os.unlink(temp, dir_fd=folder)
            except OSError:
                pass
            raise
        return None
    finally:
        os.close(folder)


def apply(fixes: list[Fix]) -> tuple[list[Fix], list[Skipped]]:
    """Write or delete each fix's file, only when it still holds the bytes `findings` read and sits directly in a
    real `.agentflow/prompts/` folder. A file that fails is skipped with the reason; the others still go ahead.
    Returns the fixes applied and the files skipped."""
    applied: list[Fix] = []
    skipped: list[Skipped] = []
    for fix in fixes:
        if not _inside_prompts(fix.path):
            skipped.append(Skipped(fix.path, "outside .agentflow/prompts"))
            continue
        try:
            reason = _apply_one(fix)
        except OSError as exc:
            reason = f"could not write: {exc.strerror or exc}"
        if reason is None:
            applied.append(fix)
        else:
            skipped.append(Skipped(fix.path, reason))
    return applied, skipped
