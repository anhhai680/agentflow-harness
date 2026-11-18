"""The run brief: the stable text of a phase and role, which a Claude run session gets as an appended system prompt.

It carries the repository's agent rules, the engine preamble, the step's skill and `brief:` files, and the result
contract. Everything taken from the repository is read **at the run's fork commit** (`base_sha`) in the main
checkout through `gitops.read_blob`, which proves each object against its id: no agent of the run can change that
commit, so nothing an agent writes in its worktree or in the main checkout's working tree reaches a later session's
system prompt. The engine's own prompts and bundled skills come from the engine's files.

`render` is pure: equal parts give a byte-identical brief, so later sessions read it from the prompt cache.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agentflow import gitops, pack
from agentflow.conditions import normalise_skill
from agentflow.config import AGENT_RULE_MAX_BYTES, memory_blockers, rules_path_problem

BRIEF_FILE_MAX_BYTES = AGENT_RULE_MAX_BYTES  # 200_000: a file larger than this is not carried (validate warns at it)
BRIEF_MAX_CHARS = 150_000  # a whole brief longer than this is not used

RULES_HEADING = "# The repository's agent rules"
RULES_LEAD = ("These are the repository's agent rules as of the commit this run started from (`{sha12}`). "
              "Claude Code does not load `CLAUDE.md` files itself in this session: follow these.")
SKILL_LEAD = "The step's skill and brief files follow in full, so you already have them: do not load them."

_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")  # a full commit id: a short prefix could be ground to a forged commit


@dataclass(frozen=True)
class Doc:
    heading: str
    text: str


def _full(base_sha: object) -> bool:
    """Whether `base_sha` is a full commit id (never a ref name, never a prefix)."""
    return isinstance(base_sha, str) and _SHA.fullmatch(base_sha) is not None


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n")


def read_rules(checkout: Path, base_sha: str | None, files: Sequence[str]) -> tuple[list[Doc], bool]:
    """`(docs, carries)`: the rules files at `base_sha`, in the order listed, and whether the brief carries the
    repository's rules faithfully. `([], False)` means it does not, and the session lets Claude load its own memory
    files, as before: no full `base_sha`, no files, a listed path `project.agent_rules` refuses, a tree that can't be
    proven (`gitops.verified_tree_paths`), a memory file the brief would not replace (`memory_blockers`), or a
    listed file in the tree that `read_blob` refuses (too big, not a regular file, a symlink out of the repository or
    under `.agentflow`, a tampered object). A listed file that is not in the tree is left out. `validate`'s
    `project.agent_rules` warnings check the same things at `HEAD`, symlinks included, from the same bounded
    `verified_tree_paths` listing."""
    if not _full(base_sha) or not files or any(rules_path_problem(entry) for entry in files):
        return [], False
    rules = list(dict.fromkeys(PurePosixPath(entry).as_posix() for entry in files))
    # Proven, not `tree_paths`: a rewritten subtree could otherwise hide a memory file or a listed rules file.
    tree = gitops.verified_tree_paths(checkout, base_sha)
    if tree is None:
        return [], False
    listed = set(tree)
    texts: dict[str, str | None] = {}

    def read(rule: str) -> str | None:
        if rule not in listed:
            return None
        if rule not in texts:
            data = gitops.read_blob(checkout, base_sha, rule, BRIEF_FILE_MAX_BYTES)
            texts[rule] = None if data is None else _text(data)
        return texts[rule]

    if memory_blockers(tree, read, rules):
        return [], False
    docs: list[Doc] = []
    for rule in rules:
        if rule not in listed:
            continue
        text = read(rule)
        if text is None:
            return [], False
        docs.append(Doc(f"## {rule}", text))
    return docs, True


def _skill_path(skills_dir: object, name: object) -> str | None:
    """`<skills_dir>/<name>/SKILL.md` as a repository path, under the containment `pack.skill_file` applies."""
    if not isinstance(name, str) or name in (".", "..") or pack.SKILL_NAME.fullmatch(name) is None:
        return None
    if not isinstance(skills_dir, str) or not skills_dir or "\\" in skills_dir or "\0" in skills_dir:
        return None
    relative = PurePosixPath(skills_dir)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        return None
    return "/".join((*relative.parts, name, "SKILL.md"))


def read_skill(checkout: Path, base_sha: str | None, skills_dir: str, name: str, repo_owned: bool) -> Doc | None:
    """Skill `name` as a brief doc. A repository skill (`repo_owned`) is read at `base_sha`; None when it can't be,
    and the skill then stays in the user prompt. A bundled skill is read from the engine's files."""
    path = _skill_path(skills_dir, name)
    if path is None:
        return None
    if repo_owned:
        data = gitops.read_blob(checkout, base_sha, path, BRIEF_FILE_MAX_BYTES) if _full(base_sha) else None
        if data is None:
            return None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:  # as `skill_text` reads it: an undecodable skill is no text
            return None
    else:
        try:
            text = (pack.ENGINE_SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
    return Doc(f"## The `{name}` skill", normalise_skill(text))


def read_brief_files(checkout: Path, base_sha: str | None, files: Sequence[str]) -> list[Doc] | None:
    """The step's `brief:` files at `base_sha`, in order; None when any can't be read, and they then stay in the
    user prompt."""
    docs: list[Doc] = []
    for name in files:
        data = gitops.read_blob(checkout, base_sha, name, BRIEF_FILE_MAX_BYTES) if _full(base_sha) else None
        if data is None:
            return None
        docs.append(Doc(f"## {name}", _text(data)))
    return docs


def _docs(docs: list[Doc]) -> str:
    return "\n\n".join(f"{doc.heading}\n\n{doc.text.strip(chr(10))}" for doc in docs)


def render(*, rules: list[Doc], base_sha: str, preamble: str, skill: list[Doc], result: str) -> str:
    """The brief: the rules (when any), the preamble, the skill and brief files (when any), then the result
    contract, each part separated by a blank line and the whole ending with one newline. Pure."""
    parts: list[str] = []
    if rules:
        parts.append(f"{RULES_HEADING}\n\n{RULES_LEAD.format(sha12=base_sha[:12])}\n\n{_docs(rules)}")
    parts.append(preamble)
    if skill:
        parts.append(f"{SKILL_LEAD}\n\n{_docs(skill)}")
    parts.append(result)
    return "\n\n".join(part.strip("\n") for part in parts) + "\n"


def digest(text: str) -> str:
    """The full SHA-256 hex of `text`."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_name(phase: str, role: str, text: str) -> str:
    """`<phase>-<role>-<digest12>.md`, with any character of `phase` or `role` outside `[A-Za-z0-9_-]` made `_`."""
    return f"{_NAME_UNSAFE.sub('_', phase)}-{_NAME_UNSAFE.sub('_', role)}-{digest(text)[:12]}.md"
