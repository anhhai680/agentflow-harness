"""Small git and GitHub helpers shared by conditions and the runner."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from agentflow.guards import check_message, path_matches
from agentflow.pack import LESSONS_FILE

if TYPE_CHECKING:
    from agentflow.config import Config


class GitError(Exception):
    """A git command the runner needed failed."""


class CommitRejected(GitError):
    """The commit would break a guardrail: a rejected trailer, a forbidden path in the change, or `HEAD`
    not where the runner left it.

    `kind` is the guard's violation kind (`rejected_trailer`, `forbidden_path` or `agent_commit`).
    """

    def __init__(self, message: str, kind: str = "rejected_trailer") -> None:
        super().__init__(message)
        self.kind = kind


class NothingToCommit(GitError):
    """`git add -A` staged nothing, so there is no commit to make."""


class GhError(Exception):
    """`gh` is missing, exited non-zero, timed out or printed something that is not JSON."""


def gh_json(args: list[str], cwd: Path | None = None) -> Any:
    """Run `gh <args>` and return its parsed JSON output."""
    try:
        proc = subprocess.run(
            ["gh", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=60,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhError(f"gh could not run: {exc}") from exc
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip() or f"gh exited {proc.returncode}")
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise GhError(f"gh printed invalid JSON: {exc}") from exc


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout


def _git_or_raise(repo: Path, *args: str) -> str:
    """`_git`, with a failure turned into `GitError` carrying git's own message."""
    try:
        return _git(repo, *args)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip() or f"exit {exc.returncode}"
        raise GitError(f"git {' '.join(args)} failed: {detail}") from exc


def _gh(args: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhError(f"gh could not run: {exc}") from exc


def pr_checks(worktree: Path, url: str) -> list[dict] | None:
    """The pull request's checks (`name`, `bucket`, `link`); None when GitHub reports none yet.

    The verdict is each check's `bucket`, never the exit code: with `--json`, a failed check still exits 0 (seen
    on real pull requests with gh 2.103), and the plain form's documented 1 and 8 are accepted too. No checks is
    exit 1 and a line on stderr, with nothing on stdout."""
    proc = _gh(["pr", "checks", url, "--json", "name,state,bucket,link"], Path(worktree))
    if "no checks reported" in proc.stderr:
        return None
    if proc.returncode not in (0, 1, 8) or not proc.stdout.strip():
        raise GhError(proc.stderr.strip() or f"gh exited {proc.returncode}")
    try:
        checks = json.loads(proc.stdout)
    except ValueError as exc:
        raise GhError(f"gh printed invalid JSON: {exc}") from exc
    if not isinstance(checks, list):
        raise GhError("gh pr checks did not print a list")
    return checks


def job_log(worktree: Path, job_id: str) -> str:
    """The failed steps' log of one Actions job, or a line saying why it cannot be had."""
    proc = _gh(["run", "view", "--job", job_id, "--log-failed"], Path(worktree), timeout=180)
    return proc.stdout if proc.returncode == 0 else f"(gh could not fetch this log: {proc.stderr.strip()})"


def rerun_failed(worktree: Path, run_id: str) -> None:
    """Run the failed jobs of one Actions run again."""
    proc = _gh(["run", "rerun", run_id, "--failed"], Path(worktree))
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip() or f"gh exited {proc.returncode}")


def update_pr_body(worktree: Path, url: str, body_file: Path) -> None:
    proc = _gh(["pr", "edit", url, "--body-file", str(body_file)], Path(worktree))
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip() or f"gh exited {proc.returncode}")


def pr_mergeable(worktree: Path, url: str) -> str:
    """GitHub's verdict on merging the pull request into its target: `MERGEABLE`, `CONFLICTING` or `UNKNOWN`
    (GitHub has not worked it out yet). Reads only."""
    proc = _gh(["pr", "view", url, "--json", "mergeable,mergeStateStatus"], Path(worktree))
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip() or f"gh exited {proc.returncode}")
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise GhError(f"gh printed invalid JSON: {exc}") from exc
    return str((data or {}).get("mergeable") or "UNKNOWN").upper() if isinstance(data, dict) else "UNKNOWN"


def mark_pr_ready(worktree: Path, url: str) -> None:
    """Take the pull request out of draft. This is as far as the runner goes: a human merges."""
    proc = _gh(["pr", "ready", url], Path(worktree))
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip() or f"gh exited {proc.returncode}")


def changed_files(repo: Path, base_ref: str) -> list[str] | None:
    """Paths that differ from where `HEAD` forked from `base_ref`: committed, staged, unstaged and untracked.

    Returns None when git cannot tell (no such ref, no commits, not a checkout).
    """
    try:
        base = _git(repo, "merge-base", base_ref, "HEAD").strip()
        tracked = _git(repo, "diff", "--name-only", "--no-renames", "-z", base)
        untracked = _git(repo, "ls-files", "-o", "--exclude-standard", "-z")
    except (subprocess.CalledProcessError, OSError):
        return None
    return sorted({p for p in (tracked + untracked).split("\0") if p})


def head(worktree: Path) -> str:
    """The full sha `HEAD` points at."""
    return _git(Path(worktree), "rev-parse", "HEAD").strip()


def commit(worktree: Path, message: str, cfg: Config, paths: list[str] | None = None,
           expected_head: str | None = None) -> str:
    """Stage everything and commit it with `project.commit_trailer` appended; return the new sha.

    The message is checked against `guardrails.reject_trailers` before anything is staged, so a
    rejected trailer raises `CommitRejected` and leaves the index alone. A staged path matching
    `guardrails.forbidden_paths` also raises `CommitRejected` (kind `forbidden_path`), after the
    index is reset to `HEAD` (the working tree is untouched): the runner never commits those files,
    whoever changed them. An empty change raises
    `NothingToCommit`. The runner never amends, so this always makes a new commit.

    With `paths`, only those paths are staged and committed (a decision recorded in the middle of a
    task must not take the task's unfinished work with it).

    With `expected_head` (the last `HEAD` the runner recorded), a `HEAD` that moved since (a gate or a test
    command ran code that committed) raises `CommitRejected` (kind `agent_commit`) before anything is
    staged. The commit runs with no hooks (`core.hooksPath=/dev/null`, `--no-verify`), and the message of
    the commit actually created is checked again: a rejected trailer in it raises `CommitRejected` naming
    the commit, which is left in place for the human (never rolled back silently).
    """
    worktree = Path(worktree)
    before = head(worktree)
    if expected_head is not None and before != expected_head:
        raise CommitRejected(f"HEAD moved from {expected_head[:12]} to {before[:12]} since the runner last recorded "
                             "it; the runner commits only on top of its own commits", kind="agent_commit")
    text = message.strip()
    trailer = cfg.project.commit_trailer.strip()
    if trailer and not text.endswith(trailer):
        text = f"{text}\n\n{trailer}"
    rejected = check_message(cfg, text)
    if rejected:
        raise CommitRejected("commit message carries a rejected trailer: " + ", ".join(v.detail for v in rejected))
    only = ["--", *paths] if paths is not None else []
    if paths is not None and not paths:
        raise NothingToCommit(f"no paths to commit in {worktree}")
    _git(worktree, "add", "-A", *only)
    staged = [p for p in _git(worktree, "diff", "--cached", "--name-only", "--no-renames", "-z", *only).split("\0") if p]
    forbidden = [p for p in staged if any(path_matches(p, g) for g in cfg.guardrails.forbidden_paths)]
    if forbidden:
        _git(worktree, "reset", "-q")
        raise CommitRejected("commit would include forbidden path(s): " + ", ".join(forbidden), kind="forbidden_path")
    if not staged:
        raise NothingToCommit(f"nothing to commit in {worktree}")
    proc = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "commit", "--no-verify", "-q", "-F", "-", *only],
        cwd=worktree, input=text + "\n", capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git commit exited {proc.returncode}")
    sha = head(worktree)
    parent = _git_or_raise(worktree, "rev-parse", f"{sha}^").strip()
    created = _git_or_raise(worktree, "log", "-1", "--format=%B", sha)
    rejected = check_message(cfg, created)
    if rejected or parent != before:
        why = ("its message carries a rejected trailer: " + ", ".join(v.detail for v in rejected)) if rejected \
            else f"its parent is {parent[:12]}, not {before[:12]}"
        raise CommitRejected(f"commit {sha[:12]} was created, but {why}; it is left in place for a human to "
                             "inspect (nothing was rolled back)", kind="rejected_trailer" if rejected else "agent_commit")
    return sha


def _status_entries(worktree: Path) -> list[tuple[str, str]]:
    """`(XY, path)` for every changed, staged or untracked path, read from `git status --porcelain -z`.

    Renames are turned off, and should git still report one (`R` or `C`), its source path is kept as an entry
    of its own, so no path a change touches goes unseen. Paths are raw bytes decoded as git printed them: no
    quoting, so spaces, newlines and a leading `-` arrive as they are."""
    out = _git_or_raise(worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames",
                        "--ignore-submodules=none")
    fields = out.split("\0")
    entries: list[tuple[str, str]] = []
    i = 0
    while i < len(fields):
        item = fields[i]
        i += 1
        if not item:
            continue
        xy, path = item[:2], item[3:]
        entries.append((xy, path))
        if "R" in xy or "C" in xy:
            if i < len(fields):
                entries.append((xy, fields[i]))
            i += 1
    return entries


def _lessons_refused(worktree: Path, why: str) -> CommitRejected:
    """Put the index back at `HEAD` (the working tree is untouched) and name the refusal, as `commit` does."""
    _git(worktree, "reset", "-q")
    return CommitRejected(f"the lessons commit was refused: {why}", kind="forbidden_path")


def commit_lessons(worktree: Path, message: str, cfg: Config) -> str:
    """Commit `pack.LESSONS_FILE`, and nothing else, with `project.commit_trailer` appended; return the new sha.

    This is the one runner commit that may touch the pack: `commit` refuses every `.agentflow/**` path, and
    this one allows exactly the lessons file, as a regular file (mode 100644) inside the worktree.

    In order: the message is checked against `guardrails.reject_trailers` before anything is staged (a rejected
    trailer raises `CommitRejected` and changes nothing). Any other changed, staged or untracked path, a type
    change, a symlink at the lessons file or at its directory, or a lessons path that is not a regular file
    inside the worktree raises `CommitRejected` (kind `forbidden_path`) after the index is reset to `HEAD`.
    An unchanged or absent lessons file raises `NothingToCommit`. The commit runs with no hooks
    (`core.hooksPath=/dev/null`, `--no-verify`) and is limited to the lessons path. The commit actually created
    is checked again: one parent, the old `HEAD`; exactly the lessons file changed, as a regular file; no
    rejected trailer. A failure raises `CommitRejected` naming the commit, which is left in place for a human
    (never rolled back silently)."""
    worktree = Path(worktree)
    before = head(worktree)
    text = message.strip()
    trailer = cfg.project.commit_trailer.strip()
    if trailer and not text.endswith(trailer):
        text = f"{text}\n\n{trailer}"
    rejected = check_message(cfg, text)
    if rejected:
        raise CommitRejected("commit message carries a rejected trailer: " + ", ".join(v.detail for v in rejected))

    entries = _status_entries(worktree)
    others = sorted({path for _, path in entries if path != LESSONS_FILE})
    if others:
        raise _lessons_refused(worktree, "it may change only " + LESSONS_FILE + ", and these changed too: "
                               + ", ".join(repr(p) for p in others))
    if any("T" in xy or "U" in xy for xy, _ in entries):
        raise _lessons_refused(worktree, f"{LESSONS_FILE} changed type or is unmerged")
    lessons = worktree / LESSONS_FILE
    if lessons.parent.is_symlink() or lessons.is_symlink():
        raise _lessons_refused(worktree, f"{LESSONS_FILE} or its directory is a symlink")
    if not entries or not os.path.lexists(lessons):
        raise NothingToCommit(f"{LESSONS_FILE} is unchanged or absent in {worktree}")
    if not lessons.is_file() or lessons.resolve() != worktree.resolve() / LESSONS_FILE:
        raise _lessons_refused(worktree, f"{LESSONS_FILE} is not a regular file inside the worktree")

    _git_or_raise(worktree, "add", "--", LESSONS_FILE)
    raw = [f for f in _git(worktree, "diff", "--cached", "--raw", "-z", "--no-renames", "--no-abbrev").split("\0") if f]
    staged = raw[1::2]
    if not staged:
        raise NothingToCommit(f"{LESSONS_FILE} is unchanged in {worktree}")
    if staged != [LESSONS_FILE]:
        raise _lessons_refused(worktree, "the index holds other paths: " + ", ".join(repr(p) for p in staged))
    if raw[0].split()[1] != "100644":
        raise _lessons_refused(worktree, f"{LESSONS_FILE} would be committed with mode {raw[0].split()[1]}, "
                               "not as a regular file (100644)")

    proc = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "commit", "--no-verify", "-q", "-F", "-", "--", LESSONS_FILE],
        cwd=worktree, input=text + "\n", capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git commit exited {proc.returncode}")

    sha = head(worktree)
    parents = _git_or_raise(worktree, "rev-list", "--parents", "-n", "1", sha).split()[1:]
    changed = [p for p in _git_or_raise(worktree, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--no-renames",
                                        sha).split("\0") if p]
    entry = _git_or_raise(worktree, "ls-tree", "-z", sha, "--", LESSONS_FILE).split()
    created = _git_or_raise(worktree, "log", "-1", "--format=%B", sha)
    rejected = check_message(cfg, created)
    if rejected:
        why, kind = "its message carries a rejected trailer: " + ", ".join(v.detail for v in rejected), "rejected_trailer"
    elif parents != [before]:
        why, kind = f"its parents are {', '.join(p[:12] for p in parents) or 'none'}, not {before[:12]}", "agent_commit"
    elif changed != [LESSONS_FILE]:
        why, kind = "it changes " + ", ".join(repr(p) for p in changed) + f", not only {LESSONS_FILE}", "forbidden_path"
    elif not entry or entry[0] != "100644":
        why, kind = f"it records {LESSONS_FILE} with mode {entry[0] if entry else 'none'}, not 100644", "forbidden_path"
    else:
        return sha
    raise CommitRejected(f"commit {sha[:12]} was created, but {why}; it is left in place for a human to inspect "
                         "(nothing was rolled back)", kind=kind)


def commits_between(worktree: Path, base: str, tip: str = "HEAD", exclude: Sequence[str] = ()) -> list[str]:
    """The full shas reachable from `tip` but not from `base` (`git rev-list base..tip`); `GitError` if unknown.

    Commits reachable from any of `exclude` are left out too: the target branch's own commits, once it is
    brought into the branch."""
    return _git_or_raise(Path(worktree), "rev-list", f"{base}..{tip}", *[f"^{sha}" for sha in exclude]).split()


# --------------------------------------------------------------------------- bringing the target branch in


@dataclass
class PullResult:
    """What `pull_base` found: `up_to_date`; `merged` (clean, left uncommitted, `target_sha` is the tip brought in);
    or `conflicted` (`conflicts` lists the unresolved paths; the work tree holds the conflict markers)."""

    status: Literal["up_to_date", "merged", "conflicted"]
    target_sha: str = ""
    conflicts: list[str] = field(default_factory=list)


_MARKER = re.compile(r"^(?:<{7}|>{7})(?: .*)?$", re.MULTILINE)


def merge_in_progress(worktree: Path) -> bool:
    """Whether the checkout holds a started, not yet committed, merge (`MERGE_HEAD` exists)."""
    return subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=worktree,
                          capture_output=True, check=False).returncode == 0


def merge_target(worktree: Path) -> str:
    """The tip of the branch being brought in by the merge in progress, or "" when there is none."""
    proc = subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=worktree,
                          capture_output=True, text=True, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def unmerged_paths(worktree: Path) -> list[str]:
    """Paths with unresolved conflicts in the index."""
    out = _git_or_raise(Path(worktree), "diff", "--name-only", "--diff-filter=U", "-z")
    return sorted(p for p in out.split("\0") if p)


def pull_base(worktree: Path, target: str, timeout: int = 180) -> PullResult:
    """Fetch `origin/<target>` and start bringing it into the checked-out branch, without committing.

    A clean result stays staged and a conflicted one stays in the tree, so the caller can check the result
    before anything is committed. Never forced, no hooks, no prompts. `GitError` when git could not fetch or
    could not start (no network, an unknown branch, local changes the merge would overwrite)."""
    worktree = Path(worktree)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    args = ["git", "-c", "core.hooksPath=/dev/null", "pull", "-q", "--no-rebase", "--no-ff", "--no-commit",
            "--no-edit", "origin", target]
    try:
        proc = subprocess.run(args, cwd=worktree, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                              stdin=subprocess.DEVNULL, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"could not bring origin/{target} in: {exc}") from exc
    if merge_in_progress(worktree):
        conflicts = unmerged_paths(worktree)
        return PullResult("conflicted" if conflicts else "merged", merge_target(worktree), conflicts)
    if proc.returncode != 0:
        detail = next((ln.strip() for ln in reversed((proc.stderr or proc.stdout).splitlines()) if ln.strip()), "")
        raise GitError(f"could not bring origin/{target} in: {detail or f'git exited {proc.returncode}'}")
    return PullResult("up_to_date")


def conflict_markers(worktree: Path, paths: Sequence[str]) -> list[str]:
    """The `paths` (relative to the checkout) that still hold a conflict marker line (`<<<<<<<` or `>>>>>>>`)."""
    left = []
    for rel in paths:
        try:
            text = (Path(worktree) / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue  # resolved by deleting the file
        if _MARKER.search(text):
            left.append(rel)
    return left


def commit_sync(worktree: Path, message: str, cfg: Config, target_sha: str, expected_head: str | None = None) -> str:
    """Conclude the merge in progress with a commit (the trailer appended) and return its sha.

    Like `commit`, with these differences: everything is staged, and the forbidden-path check applies to what the
    merge itself decided, that is the paths whose content equals neither the branch's (`HEAD`) nor the target's
    (`target_sha`). A forbidden file that only came in from the target, or that only the branch changed, passes.
    A rejected commit leaves the merge in progress."""
    worktree = Path(worktree)
    before = head(worktree)
    if expected_head is not None and before != expected_head:
        raise CommitRejected(f"HEAD moved from {expected_head[:12]} to {before[:12]} since the runner last recorded "
                             "it; the runner commits only on top of its own commits", kind="agent_commit")
    if merge_target(worktree) != target_sha:
        raise CommitRejected(f"there is no merge of {target_sha[:12]} in progress", kind="agent_commit")
    text = message.strip()
    trailer = cfg.project.commit_trailer.strip()
    if trailer and not text.endswith(trailer):
        text = f"{text}\n\n{trailer}"
    rejected = check_message(cfg, text)
    if rejected:
        raise CommitRejected("commit message carries a rejected trailer: " + ", ".join(v.detail for v in rejected))
    _git_or_raise(worktree, "add", "-A")
    left = unmerged_paths(worktree)
    if left:
        raise CommitRejected("unresolved conflicts: " + ", ".join(left), kind="agent_commit")

    def differing(sha: str) -> set[str]:
        out = _git_or_raise(worktree, "diff", "--cached", "--name-only", "--no-renames", "-z", sha)
        return {p for p in out.split("\0") if p}

    decided = differing(before) & differing(target_sha)
    forbidden = sorted(p for p in decided if any(path_matches(p, g) for g in cfg.guardrails.forbidden_paths))
    if forbidden:
        raise CommitRejected("the merge resolution would change forbidden path(s): " + ", ".join(forbidden),
                             kind="forbidden_path")
    proc = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "commit", "--no-verify", "-q", "-F", "-"],
        cwd=worktree, input=text + "\n", capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git commit exited {proc.returncode}")
    sha = head(worktree)
    parents = _git_or_raise(worktree, "rev-list", "--parents", "-n", "1", sha).split()[1:]
    if parents != [before, target_sha]:
        raise CommitRejected(f"commit {sha[:12]} was created, but its parents are not the branch and the target; it is "
                             "left in place for a human to inspect (nothing was rolled back)", kind="agent_commit")
    return sha


def branch_exists(repo: Path, name: str) -> bool:
    return subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], cwd=repo, capture_output=True, check=False
    ).returncode == 0


def add_worktree(repo: Path, path: Path, branch: str, base: str) -> None:
    """Create a linked worktree at `path` on a new `branch` forked from `base`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _git_or_raise(Path(repo), "worktree", "add", "-q", "-b", branch, str(path), base)


def sync_branch(repo: Path, branch: str, timeout: int = 60) -> str:
    """Bring local `branch` up to origin's, fast-forward only. Returns "" when it did (or had nothing to do),
    else one line saying why not. It never raises and never rewrites history: git itself refuses a branch that
    diverged, and a checked-out branch whose local edits the merge would overwrite."""
    repo = Path(repo)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8",
                              timeout=timeout, stdin=subprocess.DEVNULL, env=env, check=False)

    try:
        current = git("symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
        if current == branch:
            proc = git("pull", "-q", "--ff-only", "origin", branch)
        else:
            proc = git("fetch", "-q", "origin", f"{branch}:{branch}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"could not update {branch} from origin: {exc}"
    if proc.returncode == 0:
        return ""
    detail = next((ln.strip() for ln in reversed((proc.stderr or proc.stdout).splitlines()) if ln.strip()), "")
    return f"could not update {branch} from origin: {detail or f'git exited {proc.returncode}'}"


def fetch_remote_branch(repo: Path, branch: str, timeout: int = 60) -> bool:
    """Update `origin/<branch>` only (no local branch, no work tree). False when git could not."""
    try:
        return subprocess.run(
            ["git", "fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], cwd=repo,
            capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, check=False,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


BLOB_SKIP = 4 << 20  # an entry larger than this is left out
BLOBS_MAX = 500  # kept entries read from one directory
BYTES_MAX = 16 << 20  # total size of the distinct blobs read from one directory
GIT_READ_TIMEOUT_S = 60
_DRAIN_CHUNK = 64 << 10


@dataclass(frozen=True)
class Blob:
    text: str
    truncated: bool


@dataclass(frozen=True)
class Listing:
    blobs: dict[str, Blob]
    complete: bool


def blobs_at(repo: Path, ref: str, directory: str, keep: Callable[[str], bool], limit: int) -> Listing | None:
    """The regular files directly in `directory` at `ref` whose basename passes `keep`, each cut to `limit` bytes.

    Streams `git cat-file --batch`, so memory per blob stays at `limit`. `None` when git could not read (missing ref,
    git failed, or timed out); an empty complete `Listing` when only the directory is missing. `complete` is False when
    an entry was left out for size, count or byte budget, or because its blob was missing.
    """
    directory = directory.strip("/")
    try:
        proc = subprocess.run(
            ["git", "ls-tree", "-z", "-l", ref, "--", f"{directory}/" if directory else "."], cwd=Path(repo),
            capture_output=True, timeout=GIT_READ_TIMEOUT_S, stdin=subprocess.DEVNULL, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    complete = True
    entries: list[tuple[str, str]] = []  # (basename, sha)
    sizes: dict[str, int] = {}
    total = 0
    for record in proc.stdout.decode("utf-8", errors="replace").split("\0"):
        meta, tab, path = record.partition("\t")
        fields = meta.split()
        if not tab or len(fields) != 4 or fields[0] not in ("100644", "100755"):
            continue
        name = path.rsplit("/", 1)[-1]
        if "\n" in name or not keep(name):
            continue
        sha, size = fields[2], int(fields[3]) if fields[3].isdigit() else -1
        if size < 0 or size > BLOB_SKIP or len(entries) >= BLOBS_MAX:
            complete = False
            continue
        if sha not in sizes:
            if total + size > BYTES_MAX:
                complete = False
                continue
            sizes[sha] = size
            total += size
        entries.append((name, sha))
    if not sizes:
        return Listing({}, complete)
    read = _cat_file_batch(Path(repo), list(sizes), limit)
    if read is None:
        return None
    blobs: dict[str, Blob] = {}
    for name, sha in entries:
        if sha in read:
            blobs[name] = read[sha]
        else:
            complete = False
    return Listing(blobs, complete)


def _cat_file_batch(repo: Path, shas: list[str], limit: int) -> dict[str, Blob] | None:
    """`{sha: Blob}` from one `git cat-file --batch`; a missing sha is absent. `None` on any failure or timeout."""
    try:
        proc = subprocess.Popen(["git", "cat-file", "--batch"], cwd=repo, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        proc.kill()

    def feed() -> None:
        try:
            assert proc.stdin is not None
            proc.stdin.write("".join(f"{sha}\n" for sha in shas).encode())
            proc.stdin.close()
        except (OSError, ValueError):
            pass  # git went away; the read below sees it

    timer = threading.Timer(GIT_READ_TIMEOUT_S, expire)
    writer = threading.Thread(target=feed, daemon=True)
    timer.start()
    writer.start()
    out: dict[str, Blob] = {}
    ok = False
    try:
        assert proc.stdout is not None
        stream = proc.stdout
        for _ in shas:
            header = stream.readline().split()
            if len(header) == 2 and header[1] == b"missing":
                continue
            if len(header) != 3 or not header[2].isdigit():
                return None
            size = int(header[2])
            body = stream.read(min(size, limit))
            if len(body) != min(size, limit):
                return None
            rest = size - len(body)
            while rest > 0:
                chunk = stream.read(min(rest, _DRAIN_CHUNK))
                if not chunk:
                    return None
                rest -= len(chunk)
            if stream.read(1) != b"\n":
                return None
            out[header[0].decode("ascii", errors="replace")] = Blob(body.decode("utf-8", errors="replace"), size > len(body))
        ok = not timed_out.is_set()
        return out if ok else None
    except (OSError, ValueError):
        return None
    finally:
        timer.cancel()
        if not ok or proc.poll() is None:
            proc.kill()
        proc.wait()
        if proc.stdout:
            proc.stdout.close()
        writer.join(timeout=1)


def changed_since(worktree: Path, sha: str) -> list[str]:
    """Files that differ from `sha`: committed since, staged, unstaged and untracked."""
    worktree = Path(worktree)
    tracked = _git_or_raise(worktree, "diff", "--name-only", "--no-renames", "-z", sha)
    untracked = _git_or_raise(worktree, "ls-files", "-o", "--exclude-standard", "-z")
    return sorted({p for p in (tracked + untracked).split("\0") if p})


def diff_stat(worktree: Path, sha: str) -> str:
    """`git diff --stat` of the work tree against `sha` (untracked files are not in it)."""
    return _git_or_raise(Path(worktree), "diff", "--stat", sha).rstrip()


def push(worktree: Path, branch: str) -> None:
    """Push `branch` to `origin` and track it. Never forced."""
    _git_or_raise(Path(worktree), "push", "-q", "-u", "origin", branch)


def restore_paths(worktree: Path, paths: list[str], sha: str) -> None:
    """Put exactly `paths` back as they are at `sha`, in the index and the tree; a path `sha` lacks is removed."""
    root = Path(worktree)
    for path in paths:
        if subprocess.run(["git", "cat-file", "-e", f"{sha}:{path}"], cwd=root, capture_output=True).returncode == 0:
            _git_or_raise(root, "restore", f"--source={sha}", "--staged", "--worktree", "--", path)
        else:
            _git_or_raise(root, "rm", "-q", "--force", "--cached", "--ignore-unmatch", "--", path)
            (root / path).unlink(missing_ok=True)


def dirty_paths(worktree: Path) -> list[str]:
    """Paths with staged, unstaged or untracked changes in the checkout at `worktree` (ignored files aside)."""
    return [line[3:] for line in _git_or_raise(Path(worktree), "status", "--porcelain").splitlines() if line.strip()]


def uncommitted_paths(worktree: Path) -> list[str]:
    """Every path with staged, unstaged or untracked changes (each untracked file, not its folder), sorted.

    Read from `git status --porcelain -z`, so odd file names come back unquoted; a rename lists both names.
    """
    out = _git_or_raise(Path(worktree), "status", "--porcelain", "-z", "--untracked-files=all")
    parts = out.split("\0")
    paths: set[str] = set()
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.add(entry[3:])
        if entry[0] in "RC" and i < len(parts) and parts[i]:
            paths.add(parts[i])  # the original name of a rename or copy follows as its own item
            i += 1
    return sorted(paths)


def create_draft_pr(worktree: Path, base: str, branch: str, title: str, body_file: Path) -> str:
    """Open a draft pull request with `gh` and return its URL (the last line `gh` prints)."""
    args = ["pr", "create", "--draft", "--base", base, "--head", branch, "--title", title, "--body-file", str(body_file)]
    try:
        proc = subprocess.run(["gh", *args], cwd=worktree, capture_output=True, text=True, encoding="utf-8",
                              timeout=120, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhError(f"gh could not run: {exc}") from exc
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip() or f"gh exited {proc.returncode}")
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        raise GhError("gh pr create printed no URL")
    return lines[-1]
