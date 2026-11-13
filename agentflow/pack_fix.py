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
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from agentflow import pack, safeio
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


def _read(path: Path) -> tuple[bytes, str] | Skipped:
    try:
        data = safeio.read_bytes(path, MAX_PROMPT_BYTES)
    except safeio.SymlinkRefused:
        return Skipped(path, "a symlink")
    except (ValueError, OSError):
        return Skipped(path, "not a regular file or larger than 1 MB")
    if data is None:
        return Skipped(path, "not a regular file or larger than 1 MB")
    try:
        return data, data.decode("utf-8")
    except UnicodeDecodeError:
        return Skipped(path, "not UTF-8")


def _engine_bytes(name: str) -> bytes | None:
    file = pack.ENGINE_PROMPTS / name
    return file.read_bytes() if file.is_file() else None


def findings(config_path: Path) -> Findings:
    """Every drift problem in the pack's own prompts (`<pack>/prompts/*.md`), with the fixes for those that have one
    right answer. Reads only; a prompt it can't read safely is `skipped`, never an issue."""
    out = Findings()
    folder = Path(config_path).parent / "prompts"
    if not folder.is_dir() or folder.is_symlink():
        return out
    for path in sorted(folder.glob("*.md")):
        read = _read(path)
        if isinstance(read, Skipped):
            out.skipped.append(read)
            continue
        before, text = read
        label = pack.prompt_label(config_path, path)
        crlf = "\r\n" in text
        changed = text
        for entry in RETIRED:
            old, new = (entry.old.replace("\n", "\r\n"), entry.new.replace("\n", "\r\n")) if crlf else (entry.old, entry.new)
            if old in changed:
                changed = changed.replace(old, new)
                does = "removes it" if not entry.new else "replaces it with the engine's current text"
                out.issues.append(Issue("warning", label, f"holds text the engine retired in {entry.retired_in} "
                                                          f"({entry.id}); validate --fix {does}", fixable=True))
        for entry in RETIRED:
            if entry.detect in changed:
                out.issues.append(Issue("warning", label, f"mentions {entry.detect!r}, which the engine retired in "
                                                          f"{entry.retired_in} ({entry.id}), in text that was edited, "
                                                          f"so validate --fix leaves it: compare it with "
                                                          f"engine:prompts/{entry.file}"))
        after = changed.encode("utf-8")
        engine = _engine_bytes(path.name)
        if changed == text:
            if engine is not None and before == engine:
                out.issues.append(Issue("warning", label, f"is identical to engine:prompts/{path.name}: it changes "
                                                          f"nothing and hides later engine fixes to that file; "
                                                          f"validate --fix deletes it", fixable=True))
                out.fixes.append(Fix(path, before, None))
            continue
        out.fixes.append(Fix(path, before, None if after == engine else after))
    return out


# ---- applying the fixes (`agentflow validate --fix`)

_PROMPT_NAME = re.compile(r"[A-Za-z0-9_.-]+\.md")


class FixRefused(Exception):
    """`validate --fix` will not touch this pack at all."""


def check_pack(repo: Path) -> None:
    """Refuse `--fix` inside an agentflow run (an agent never changes the pack), and on a pack whose folder or
    `prompts/` folder is a symlink (the writes would land outside the repository)."""
    if os.environ.get("AGENTFLOW_RUN"):
        raise FixRefused("validate --fix never runs inside an agentflow run")
    for folder in (Path(repo) / pack.PACK_DIR, Path(repo) / pack.PACK_DIR / "prompts"):
        if folder.is_symlink():
            raise FixRefused(f"{folder} is a symlink")


def _rel(path: Path, repo: Path) -> str:
    return os.path.relpath(path, repo)


def render(fix: Fix, repo: Path) -> str:
    """What the fix does, as a unified diff with `a/` and `b/` headers, or `delete <path>`."""
    rel = _rel(fix.path, repo)
    if fix.after is None:
        return f"delete {rel}\n"
    lines = difflib.unified_diff(fix.before.decode("utf-8").splitlines(keepends=True),
                                 fix.after.decode("utf-8").splitlines(keepends=True),
                                 fromfile=f"a/{rel}", tofile=f"b/{rel}")
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


def _inside_prompts(path: Path) -> bool:
    folder = path.parent
    return (bool(_PROMPT_NAME.fullmatch(path.name)) and folder.name == "prompts" and folder.parent.name == pack.PACK_DIR
            and not folder.is_symlink() and not folder.parent.is_symlink())


def apply(fixes: list[Fix]) -> tuple[list[Fix], list[Skipped]]:
    """Write or delete each fix's file, only when it still holds the bytes `findings` read and sits directly in a
    real `.agentflow/prompts/` folder. Returns the fixes applied and the files skipped."""
    applied: list[Fix] = []
    skipped: list[Skipped] = []
    for fix in fixes:
        if not _inside_prompts(fix.path):
            skipped.append(Skipped(fix.path, "outside .agentflow/prompts"))
            continue
        try:
            now = safeio.read_bytes(fix.path, MAX_PROMPT_BYTES)
        except (OSError, ValueError):
            now = None
        if now != fix.before:
            skipped.append(Skipped(fix.path, "changed since it was read"))
            continue
        if fix.after is None:
            safeio.remove(fix.path)
        else:
            safeio.write_text(fix.path, fix.after.decode("utf-8"))
        applied.append(fix)
    return applied, skipped
