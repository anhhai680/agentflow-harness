"""Small git and GitHub helpers shared by conditions and the runner."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import stat
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from agentflow.guards import HARDENED, Violation, check_message, path_matches
from agentflow.pack import LESSONS_FILE

if TYPE_CHECKING:
    from agentflow.config import Config

# `HARDENED` (no hook, no fsmonitor, no replace object) is defined in `guards`, whose `snapshot` needs it and which cannot import this
# module; every hardened git call here uses it.

# `HARDENED`, and no setting that could hide a change from `status`: every untracked file, every stat, no untracked cache.
STRICT = (*HARDENED, "-c", "status.showUntrackedFiles=all", "-c", "core.ignoreStat=false", "-c", "core.untrackedCache=false")
IGNORED_KEPT = 2000  # ignored paths a reconcile baseline records by name; past it, only their digest and count


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
    task must not take the task's unfinished work with it). They are literal pathspecs (`--literal-pathspecs`):
    a name such as `docs/[ab].md` never matches another file as a glob.

    With `expected_head` (the last `HEAD` the runner recorded), a `HEAD` that moved since (a gate or a test
    command ran code that committed) raises `CommitRejected` (kind `agent_commit`) before anything is
    staged. Every git call that reads the work tree runs with `HARDENED` (no hook, no fsmonitor command), the
    commit with `--no-verify` too, and the message of the commit actually created is checked again: a rejected
    trailer in it raises `CommitRejected` naming the commit, which is left in place for the human (never rolled
    back silently).
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
    git = (*HARDENED, "--literal-pathspecs")
    _git(worktree, *git, "add", "-A", *only)
    staged = [p for p in _git(worktree, *git, "diff", "--cached", "--name-only", "--no-renames", "-z", *only).split("\0")
              if p]
    forbidden = [p for p in staged if any(path_matches(p, g) for g in cfg.guardrails.forbidden_paths)]
    if forbidden:
        _git(worktree, *git, "reset", "-q")
        raise CommitRejected("commit would include forbidden path(s): " + ", ".join(forbidden), kind="forbidden_path")
    if not staged:
        raise NothingToCommit(f"nothing to commit in {worktree}")
    proc = subprocess.run(
        ["git", *git, "commit", "--no-verify", "-q", "-F", "-", *only],
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
    quoting, so spaces, newlines and a leading `-` arrive as they are. Read with `STRICT`, so no setting hides one."""
    out = _git_or_raise(worktree, *STRICT, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames",
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


def shown(path: str) -> str:
    """A path an agent chose, for a page or a terminal: quoted, with newlines and control characters escaped."""
    return repr(path)


def _flagged(worktree: Path) -> list[str]:
    """Index entries flagged skip-worktree (`S`) or assume-unchanged (lower case), as `ls-files -v` prints them:
    `status` does not see a change to one."""
    out = _git_or_raise(worktree, *STRICT, "ls-files", "-v", "-z")
    return sorted(e for e in out.split("\0") if e and (e[0] == "S" or e[0].islower()))


def _ignored(worktree: Path) -> list[str]:
    """Every ignored path, file by file even inside an ignored folder (`--ignored=traditional` with
    `--untracked-files=all`)."""
    out = _git_or_raise(worktree, *STRICT, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                        "--ignored=traditional", "--no-renames", "--ignore-submodules=none")
    return sorted(e[3:] for e in out.split("\0") if e.startswith("!! "))


def _digest(paths: list[str]) -> str:
    return hashlib.sha256("\0".join(paths).encode("utf-8", "surrogateescape")).hexdigest()


def reconcile_baseline(worktree: Path, branch: str | None) -> dict[str, Any]:
    """What a reconcile step's checks compare against (`outside_only`): `git_state` (read first, so no command an
    agent could have planted has run yet), the flagged index entries and the ignored paths (by name up to
    `IGNORED_KEPT`, always by digest and count). JSON-able, for the event log. Raises `GitError`/`OSError` when it
    cannot be read."""
    worktree = Path(worktree)
    state = git_state(worktree, branch)
    flagged, ignored = _flagged(worktree), _ignored(worktree)
    return {"branch": branch, "git_state": state, "flagged": flagged, "ignored_digest": _digest(ignored),
            "ignored_count": len(ignored), "ignored": ignored if len(ignored) <= IGNORED_KEPT else None}


def git_state_changes(worktree: Path, baseline: dict[str, Any]) -> list[Violation]:
    """`outside_output` violations naming what of `git_state` differs from the baseline's: the config (a filter, a
    signing program, an attribute file's setting ...), the push URL, the replace refs, or a control file
    (`info/attributes`, hooks ...).
    Read with `HARDENED` and nothing else, so a planted program never runs here; call it before any git command that
    reads the work tree. A state that cannot be read is a violation too."""
    try:
        now = git_state(Path(worktree), baseline.get("branch"))
    except (GitError, OSError, ValueError) as exc:
        return [Violation("outside_output", f"cannot read the git state, so it cannot be trusted: {exc}")]
    before = baseline.get("git_state") or {}
    out = [label for name, label in (("config", "the git config"), ("push_url", "the push URL of origin"),
                                     ("replace_refs", "the replace refs (`refs/replace`)"))
           if now.get(name) != before.get(name)]
    files_now, files_before = now.get("files") or {}, before.get("files") or {}
    out += [f"git's {name}" for name in sorted(set(files_now) | set(files_before))
            if json.dumps(files_now.get(name)) != json.dumps(files_before.get(name))]
    if not out:
        return []
    return [Violation("outside_output", "changed since the step started, so no git command reads the tree: "
                      + ", ".join(out) + ". Put it back as it was; to accept the change, retry the step "
                      "(`agentflow retry`), which takes a new baseline (an approval keeps the old one)")]


def outside_only(worktree: Path, only: str, baseline: dict[str, Any] | None = None) -> list[Violation]:
    """What makes the work tree more than an edit of the one file `only` (a repo-relative path), as `outside_output`
    violations; empty when `only` is the one path that differs from `HEAD` (or nothing does) and it is a non-empty,
    non-executable regular file inside the worktree with no other name.

    With `baseline` (`reconcile_baseline`), `git_state` is compared first and any difference is returned alone: a
    changed config or attribute file could run a program (a filter, an fsmonitor) in the very `status` that follows,
    or make an edited file read as unchanged. Then the tree is read against `HEAD` with `_status_entries` (`STRICT`:
    no hook or fsmonitor command, no setting that hides a change; renames off, so both paths of a rename are named;
    submodules included; untracked files included, ignored ones not): every path other than `only` is one violation.
    With `baseline`, so is a newly flagged index entry (an edit to a skip-worktree or assume-unchanged file is not in
    `status`) and any change to the ignored paths (a file written inside an ignored folder). `only` itself must be a
    regular file (not missing, not a symlink, not a folder, not reached through a symlinked folder, not executable, not
    a hard link) with at least one byte. A path that is not a plain relative path inside the work tree (absolute, `..`,
    `.git`, not normalised) is refused, and so is every failure to read the tree: a check that cannot run never
    passes. Every path in a detail is `shown`."""
    worktree = Path(worktree)
    parts = only.split("/") if isinstance(only, str) else []
    if (not parts or only.startswith("/") or posixpath.normpath(only) != only
            or any(part in ("", ".", "..") or part.casefold() == ".git" for part in parts)):
        return [Violation("outside_output", f"{shown(str(only))} is not a plain path inside the work tree")]
    if baseline is not None:
        changed = git_state_changes(worktree, baseline)
        if changed:
            return changed
    try:
        entries = _status_entries(worktree)
        flagged = _flagged(worktree) if baseline is not None else []
        ignored = _ignored(worktree) if baseline is not None else []
    except (GitError, OSError, ValueError) as exc:  # ValueError: a path git printed that is not UTF-8
        return [Violation("outside_output", f"cannot read the work tree's state, so it cannot be trusted: {exc}")]
    found = [Violation("outside_output", shown(path)) for path in sorted({path for _, path in entries if path != only})]
    if baseline is not None:
        known = set(baseline.get("flagged") or [])
        found += [Violation("outside_output", f"{shown(entry[2:])} is newly flagged {entry[0]!r} in the index, so "
                            "status cannot see a change to it") for entry in flagged if entry not in known]
        if _digest(ignored) != baseline.get("ignored_digest"):
            before = baseline.get("ignored")
            if isinstance(before, list):
                added, gone = sorted(set(ignored) - set(before)), sorted(set(before) - set(ignored))
                found += [Violation("outside_output", f"{shown(p)} (ignored) was written") for p in added]
                found += [Violation("outside_output", f"{shown(p)} (ignored) was removed") for p in gone]
            if not isinstance(before, list) or not (added or gone):
                found.append(Violation("outside_output", f"the ignored files changed since the step started "
                                       f"({baseline.get('ignored_count')} before, {len(ignored)} now)"))
    target = worktree / only
    try:
        info = os.lstat(target)
        inside = target.resolve(strict=True) == worktree.resolve(strict=True) / only
    except (OSError, RuntimeError):  # RuntimeError: a symlink loop
        info, inside = None, False
    if info is None or not stat.S_ISREG(info.st_mode) or not inside:
        found.append(Violation("outside_output", f"{shown(only)} was deleted or is not a regular file"))
    elif info.st_nlink != 1:
        found.append(Violation("outside_output", f"{shown(only)} is a hard link (it has other names, maybe outside "
                               "the work tree)"))
    elif info.st_mode & 0o111:
        found.append(Violation("outside_output", f"{shown(only)} is executable"))
    elif info.st_size == 0:
        found.append(Violation("outside_output", f"{shown(only)} is empty"))
    return found


def _artifact_refused(worktree: Path, why: str) -> CommitRejected:
    """Put the index back at `HEAD` (the working tree is untouched) and name the refusal."""
    _git(worktree, *HARDENED, "--literal-pathspecs", "reset", "-q")
    return CommitRejected(f"the reconcile commit was refused: {why}", kind="outside_output")


def commit_artifact(worktree: Path, message: str, cfg: Config, path: str, expected_head: str | None = None) -> str:
    """Commit the one file `path` (a reconcile step's artifact), and nothing else, with `project.commit_trailer`
    appended; return the new sha. `commit_lessons`'s checks, for a path a step's `output` names.

    In order: `HEAD` must be `expected_head` (`agent_commit`); the message must carry no rejected trailer; `path` must
    not be a forbidden path. The file is read once (no symlink followed): a non-empty, non-executable regular file
    with one name, whose bytes are hashed with no filters. Then it alone is staged (`--literal-pathspecs`): the index
    must differ from `HEAD` at exactly `path`, as mode 100644 and as that hash, else the index is reset and
    `CommitRejected` (kind `outside_output`) is raised. An unchanged file raises `NothingToCommit`. The commit is the
    index as checked (no pathspec, so git does not read the file again), with no hook and no fsmonitor; it is signed as
    the user's git config says, like every runner commit (a changed signing program is refused before this, by the
    caller's `git_state` compare). The commit actually created is checked again: one parent, the old `HEAD`;
    exactly `path` changed, as mode 100644 and the checked blob; no rejected trailer. A failure there raises
    `CommitRejected` naming the commit, which is left in place for a human (never rolled back silently), as
    `commit_lessons` does."""
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
    if any(path_matches(path, g) for g in cfg.guardrails.forbidden_paths):
        raise CommitRejected(f"commit would include forbidden path(s): {shown(path)}", kind="forbidden_path")
    try:
        fd = os.open(worktree / path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        raise _artifact_refused(worktree, f"{shown(path)} cannot be read as a regular file: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o111 or info.st_size == 0:
            raise _artifact_refused(worktree, f"{shown(path)} is not a non-empty, non-executable regular file with "
                                    "one name")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read()
    finally:
        os.close(fd)
    hashed = subprocess.run(["git", *HARDENED, "hash-object", "--no-filters", "--stdin"], cwd=worktree, input=data,
                            capture_output=True, check=False)
    if hashed.returncode != 0:
        detail = hashed.stderr.decode("utf-8", "replace").strip() or f"exit {hashed.returncode}"
        raise GitError(f"git hash-object failed: {detail}")
    wanted = hashed.stdout.decode().strip()

    git = (*HARDENED, "--literal-pathspecs")
    _git_or_raise(worktree, *git, "add", "--", path)
    raw = [f for f in _git(worktree, *git, "diff", "--cached", "--raw", "-z", "--no-renames", "--no-abbrev").split("\0")
           if f]
    staged = raw[1::2]
    if not staged:
        raise NothingToCommit(f"{shown(path)} is unchanged in {worktree}")
    if staged != [path]:
        raise _artifact_refused(worktree, "the index holds other paths: " + ", ".join(shown(p) for p in staged))
    _, mode, _, blob, *_ = raw[0].split()
    if mode != "100644":
        raise _artifact_refused(worktree, f"{shown(path)} would be committed with mode {mode}, not as a regular "
                                "file (100644)")
    if blob != wanted:
        raise _artifact_refused(worktree, f"the staged {shown(path)} (blob {blob[:12]}) is not the file the runner "
                                f"checked (blob {wanted[:12]}): it changed, or a filter rewrote it")
    proc = subprocess.run(
        ["git", *git, "commit", "--no-verify", "-q", "-F", "-"],
        cwd=worktree, input=text + "\n", capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git commit exited {proc.returncode}")

    sha = head(worktree)
    parents = _git_or_raise(worktree, *HARDENED, "rev-list", "--parents", "-n", "1", sha).split()[1:]
    changed = [p for p in _git_or_raise(worktree, *HARDENED, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z",
                                        "--no-renames", sha).split("\0") if p]
    entry = _git_or_raise(worktree, *git, "ls-tree", "-z", sha, "--", path).split()
    created = _git_or_raise(worktree, *HARDENED, "log", "-1", "--format=%B", sha)
    rejected = check_message(cfg, created)
    if rejected:
        why, kind = "its message carries a rejected trailer: " + ", ".join(v.detail for v in rejected), "rejected_trailer"
    elif parents != [before]:
        why, kind = f"its parents are {', '.join(p[:12] for p in parents) or 'none'}, not {before[:12]}", "agent_commit"
    elif changed != [path]:
        why, kind = "it changes " + ", ".join(shown(p) for p in changed) + f", not only {shown(path)}", "outside_output"
    elif not entry or entry[0] != "100644":
        why, kind = f"it records {shown(path)} with mode {entry[0] if entry else 'none'}, not 100644", "outside_output"
    elif entry[2] != blob:
        why, kind = (f"it records {shown(path)} as blob {entry[2][:12]}, not the checked and staged {blob[:12]}",
                     "outside_output")
    else:
        return sha
    raise CommitRejected(f"commit {sha[:12]} was created, but {why}; it is left in place for a human to inspect "
                         "(nothing was rolled back)", kind=kind)


def _lessons_refused(worktree: Path, why: str) -> CommitRejected:
    """Put the index back at `HEAD` (the working tree is untouched) and name the refusal, as `commit` does."""
    _git(worktree, "reset", "-q")
    return CommitRejected(f"the lessons commit was refused: {why}", kind="forbidden_path")


def commit_lessons(worktree: Path, message: str, cfg: Config, expected_text: str | None = None) -> str:
    """Commit `pack.LESSONS_FILE`, and nothing else, with `project.commit_trailer` appended; return the new sha.

    This is the one runner commit that may touch the pack: `commit` refuses every `.agentflow/**` path, and
    this one allows exactly the lessons file, as a regular file (mode 100644) inside the worktree.

    In order: the message is checked against `guardrails.reject_trailers` before anything is staged (a rejected
    trailer raises `CommitRejected` and changes nothing). Any other changed, staged or untracked path, a type
    change, a symlink at the lessons file or at its directory, a hard-linked lessons file, or a lessons path that
    is not a regular file inside the worktree raises `CommitRejected` (kind `forbidden_path`) after the index is
    reset to `HEAD`.
    An unchanged or absent lessons file raises `NothingToCommit`. The commit runs with no hooks
    (`core.hooksPath=/dev/null`, `--no-verify`) and is limited to the lessons path. The commit actually created
    is checked again: one parent, the old `HEAD`; exactly the lessons file changed, as a regular file whose blob
    is the one checked and staged (`git commit -- <path>` reads the file again, so a swap in between shows here);
    no rejected trailer. A failure raises `CommitRejected` naming the commit, which is left in place for a human
    (never rolled back silently).

    With `expected_text` (the text the caller wrote), the staged blob must be that text exactly (`git hash-object
    --no-filters` of its UTF-8 bytes), so a process that rewrote the file between the caller's write and the
    staging is refused (`forbidden_path`, the index reset). Every git call here runs with `HARDENED`."""
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
    if os.lstat(lessons).st_nlink != 1:
        raise _lessons_refused(worktree, f"{LESSONS_FILE} is a hard link (it has other names, maybe outside the worktree)")

    _git_or_raise(worktree, *HARDENED, "add", "--", LESSONS_FILE)
    raw = [f for f in _git(worktree, *HARDENED, "diff", "--cached", "--raw", "-z", "--no-renames",
                           "--no-abbrev").split("\0") if f]
    staged = raw[1::2]
    if not staged:
        raise NothingToCommit(f"{LESSONS_FILE} is unchanged in {worktree}")
    if staged != [LESSONS_FILE]:
        raise _lessons_refused(worktree, "the index holds other paths: " + ", ".join(repr(p) for p in staged))
    _, mode, _, blob, *_ = raw[0].split()
    if mode != "100644":
        raise _lessons_refused(worktree, f"{LESSONS_FILE} would be committed with mode {mode}, "
                               "not as a regular file (100644)")
    if expected_text is not None:
        hashed = subprocess.run(["git", "hash-object", "--no-filters", "--stdin"], cwd=worktree,
                                input=expected_text.encode("utf-8"), capture_output=True, check=False)
        if hashed.returncode != 0:
            _git(worktree, "reset", "-q")  # the index back at `HEAD`, as a refusal leaves it
            detail = hashed.stderr.decode("utf-8", "replace").strip() or f"exit {hashed.returncode}"
            raise GitError(f"git hash-object failed: {detail}")
        wanted = hashed.stdout.decode().strip()
        if blob != wanted:
            raise _lessons_refused(worktree, f"the staged {LESSONS_FILE} (blob {blob[:12]}) is not the text the runner "
                                   f"wrote (blob {wanted[:12]}): something changed the file after it was written")

    proc = subprocess.run(
        ["git", *HARDENED, "commit", "--no-verify", "-q", "-F", "-", "--", LESSONS_FILE],
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
    elif entry[2] != blob:
        why, kind = (f"it records {LESSONS_FILE} as blob {entry[2][:12]}, not the checked and staged {blob[:12]} "
                     "(the file changed between the check and the commit)"), "forbidden_path"
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


def push(worktree: Path, branch: str, sha: str | None = None) -> None:
    """Push `branch` to the branch of the same name on `origin` and track it. Never forced, and with an explicit
    `refs/heads/<branch>:refs/heads/<branch>` refspec, so a `remote.origin.push` (or `mirror`) setting cannot send it
    anywhere else; no hook runs (`HARDENED`, `--no-verify`), so a planted `pre-push` never runs as the runner.
    With `sha`, that commit is pushed (`<sha>:refs/heads/<branch>`, no upstream set), whatever the local branch
    points at by then."""
    ref = f"refs/heads/{branch}"
    if sha is not None:
        _git_or_raise(Path(worktree), *HARDENED, "push", "--no-verify", "-q", "origin", f"{sha}:{ref}")
        return
    _git_or_raise(Path(worktree), *HARDENED, "push", "--no-verify", "-q", "-u", "origin", f"{ref}:{ref}")


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


MARK_BYTES = 4 * 1024 * 1024  # bytes of one git control file `git_state` reads; a bigger one always reads as changed
MARK_ENTRIES = 512  # entries of a control folder (`hooks/`) it lists; more always read as changed
_INFO_FILES = ("exclude", "attributes", "sparse-checkout", "grafts")  # what of `info/` git reads (not `info/refs`)


def _changed() -> tuple:
    """A mark no other mark equals: what cannot be read in full cannot be vouched for, so it reads as changed."""
    return ("unbounded", os.urandom(16).hex())


def _file_mark(path: Path, depth: int = 0) -> tuple:
    """What a git control file is, for `git_state`: its type and the hash of its bytes (a link: its target; a folder:
    its entries). Opened without following a link and without blocking (a FIFO swapped in after the `lstat` cannot
    hang it), checked as a regular file on the open descriptor, and read in chunks up to `MARK_BYTES`."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return ("absent",)
    except OSError as exc:
        return ("unreadable", str(exc))
    if stat.S_ISLNK(info.st_mode):
        return ("symlink", os.readlink(path))
    if stat.S_ISDIR(info.st_mode):
        if depth >= 2:
            return _changed()
        try:
            children = sorted(path.iterdir())
        except OSError as exc:
            return ("unreadable", str(exc))
        if len(children) > MARK_ENTRIES:
            return _changed()
        return ("dir", tuple((child.name, _file_mark(child, depth + 1)) for child in children))
    if not stat.S_ISREG(info.st_mode):
        return ("special", stat.S_IFMT(info.st_mode))
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        return ("unreadable", str(exc))
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode):
            return ("special", stat.S_IFMT(opened.st_mode))
        if opened.st_size > MARK_BYTES:
            return _changed()
        digest = hashlib.sha256()
        total = 0
        while chunk := handle.read(64 * 1024):
            total += len(chunk)
            if total > MARK_BYTES:
                return _changed()
            digest.update(chunk)
    return ("file", opened.st_mode & 0o777, digest.hexdigest())


def _config_listing(worktree: Path, branch: str | None) -> bytes:
    """`git config --list --show-origin -z` of every scope, less the `branch.<name>.*` keys of every branch but
    `branch` (other runs push with `-u` and tools record merge bases there; none of it reaches a push of `branch`)."""
    out = subprocess.run(["git", *HARDENED, "config", "--list", "--show-origin", "-z"], cwd=worktree,
                         capture_output=True, check=False).stdout
    fields = out.split(b"\0")
    kept: list[bytes] = []
    for origin, entry in zip(fields[0::2], fields[1::2], strict=False):
        key = entry.split(b"\n", 1)[0].decode("utf-8", "surrogateescape")
        if branch is not None and key.startswith("branch.") and key.count(".") >= 2:
            name = key[len("branch."):key.rindex(".")]
            if name != branch:
                continue
        kept += [origin, entry]
    return b"\0".join(kept)


def git_state(worktree: Path, branch: str | None = None) -> dict[str, Any]:
    """The git state an agent could change to make a later runner git call do something else: the effective config of
    every scope (`git config --list --show-origin`; with `branch`, other branches' `branch.<name>.*` keys left out),
    the push URL of `origin`, and the control files of the checkout and of the repository it shares (`info/exclude`,
    `info/attributes`, `info/sparse-checkout`, `info/grafts`, `hooks/`, `commondir`, `gitdir`, the worktree's `.git`
    file), and the replace refs (`refs/replace/*`: a replaced object makes git read a forged one). The config files
    themselves are read only through the listing, which holds every value they set (and
    every value they include), so another branch's keys written to the shared `config` change nothing here. Two
    equal values mean nothing of it changed. Read with `HARDENED`; nothing it reads runs a command."""
    worktree = Path(worktree)

    def where(what: str) -> Path:
        found = Path(_git_or_raise(worktree, *HARDENED, "rev-parse", what).strip())
        return found if found.is_absolute() else (worktree / found)

    git_dir, common = where("--git-dir"), where("--git-common-dir")
    files = {"dot_git": worktree / ".git"}
    for name, base in (("git_dir", git_dir), ("common", common)):
        for rel in ("hooks", "commondir", "gitdir", *(f"info/{i}" for i in _INFO_FILES)):
            files[f"{name}/{rel}"] = base / rel
    url = subprocess.run(["git", *HARDENED, "remote", "get-url", "--push", "origin"], cwd=worktree,
                         capture_output=True, text=True, check=False).stdout.strip()
    marks = {name: _file_mark(path) for name, path in files.items() if name != "dot_git"}
    dot_git = files["dot_git"]  # a linked worktree's `.git` file; a main checkout's folder is read file by file above
    marks["dot_git"] = ("dir",) if dot_git.is_dir() and not dot_git.is_symlink() else _file_mark(dot_git)
    replaced = _git_or_raise(worktree, *HARDENED, "for-each-ref", "--format=%(refname) %(objectname)", "refs/replace")
    return {"config": hashlib.sha256(_config_listing(worktree, branch)).hexdigest(), "push_url": url, "files": marks,
            "replace_refs": hashlib.sha256(replaced.encode("utf-8", "surrogateescape")).hexdigest()}


def tree_state(worktree: Path) -> dict[str, Any]:
    """What the checkout at `worktree` holds besides `HEAD`, read so no setting can hide a change: every untracked
    and every ignored path (`status` with `showUntrackedFiles=all`, `ignoreStat=false`, no untracked cache), each
    index entry flagged skip-worktree or assume-unchanged (`ls-files -v`), whether the work tree and the index
    differ from `HEAD`, and `HEAD` itself: its commit and the branch it names. Two equal values mean nothing git can
    see changed."""
    worktree = Path(worktree)
    strict = STRICT
    status = _git_or_raise(worktree, *strict, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                           "--ignored=matching", "--no-renames", "--ignore-submodules=none")
    flagged = [entry for entry in _git_or_raise(worktree, *strict, "ls-files", "-v", "-z").split("\0")
               if entry and (entry[0] == "S" or entry[0].islower())]

    def differs(*args: str) -> bool:
        return subprocess.run(["git", *strict, "diff", "--quiet", *args], cwd=worktree, capture_output=True,
                              check=False).returncode != 0

    return {"status": sorted(e for e in status.split("\0") if e), "flagged": sorted(flagged),
            "worktree_differs": differs("HEAD"), "index_differs": differs("--cached", "HEAD"),
            "head": _git_or_raise(worktree, *strict, "rev-parse", "HEAD").strip(),
            "symbolic_head": subprocess.run(["git", *strict, "symbolic-ref", "-q", "HEAD"], cwd=worktree,
                                            capture_output=True, text=True, check=False).stdout.strip()}


def blob_at(repo: Path, ref: str, path: str, limit: int) -> bytes | None:
    """The bytes of `path` at commit `ref`, or None when `ref` has no such path. `ValueError` when it is not a regular
    file there (mode 100644 or 100755: no link, no tree, no submodule) or is larger than `limit` bytes."""
    repo = Path(repo)
    entry = _git_or_raise(repo, *HARDENED, "ls-tree", "-l", "-z", ref, "--", path).split("\0")[0]
    if not entry:
        return None
    meta, _tab, _name = entry.partition("\t")
    mode, kind, blob, size = (meta.split() + ["", "", "", ""])[:4]
    if kind != "blob" or mode not in ("100644", "100755"):
        raise ValueError(f"{path} at {ref[:12]} is not a regular file (mode {mode} {kind})")
    if not size.isdigit() or int(size) > limit:
        raise ValueError(f"{path} at {ref[:12]} is larger than {limit} bytes")
    return subprocess.run(["git", *HARDENED, "cat-file", "blob", blob], cwd=repo, capture_output=True,
                          check=True).stdout


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
