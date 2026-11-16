"""A run's artifacts, listed, read and diffed from the run's own commits in git, for the dashboard.

- `run_artifacts(run, repo)`: every file a phase's commits touched, by phase, each with its revisions (one per
  `step_committed` of that phase whose commit touched it). Cached per event log, like `views._METRICS_CACHE`.
- `read_artifact(run, repo, path, sha, max_bytes)`: one revision of one listed file, cut at a line past the cap.
- `artifact_diff(run, repo, path, from_sha, to_sha, max_bytes)`: `git diff` between two revisions of one file.
- `artifact_max_bytes(cfg)` is the cap (`dashboard.artifact_max_kb`), and `artifact_tabs(phases)` the kinds a lane has.

What a browser may ask for is allow-listed: the path must be one of the run's listed files and the sha one of that
file's revisions (or `head`, the run branch's tip), checked before git runs. Git always runs read-only, hardened
(`guards.HARDENED`, no stdin, a timeout, `--` before paths, literal pathspecs, no external diff or textconv), and its
output is read to the cap and no further. Only a regular file is read: a symlink, a submodule or a directory is
refused, so nothing outside git's objects is ever shown. The worktree is never read, so an artifact still reads after
its worktree is gone; a revision git no longer has is `NotFound`, never an error.
"""

from __future__ import annotations

import copy
import re
import subprocess
import threading
from pathlib import Path

from agentflow.config import Config
from agentflow.gitops import GIT_READ_TIMEOUT_S
from agentflow.guards import HARDENED
from agentflow.state import Run

PHASE_KINDS: dict[str, str] = {"define": "spec", "plan": "plan"}  # any other phase's files are kind "file"
DEFAULT_MAX_KB = 512
MIN_KB, MAX_KB = 16, 8192  # the range `validate` accepts for `dashboard.artifact_max_kb`
SHA = re.compile(r"^[0-9a-f]{7,64}$")
HEAD = "head"

_CACHE: dict[tuple, list[dict]] = {}  # run_artifacts by (repo, events file, size, mtime_ns)
_CACHE_MAX = 128
_CHUNK = 64 << 10


class NotFound(LookupError):
    """What the dashboard answers 404 with; the message is the page's text."""


def artifact_max_bytes(cfg: Config) -> int:
    """The read and diff cap in bytes. A value `validate` would refuse falls back to the default."""
    value = (cfg.dashboard or {}).get("artifact_max_kb", DEFAULT_MAX_KB)
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_KB <= value <= MAX_KB:
        value = DEFAULT_MAX_KB
    return 1024 * value


def artifact_tabs(phases: list[str]) -> list[str]:
    """The artifact kinds of a lane whose phases are `phases`, in `PHASE_KINDS` order."""
    return [kind for phase, kind in PHASE_KINDS.items() if phase in phases]


# -- git


def _git(repo: Path, *args: str, limit: int | None = None) -> tuple[bytes, bool]:
    """`(stdout, cut)` of a read-only `git <args>`. With `limit`, at most `limit` bytes are read; past that the
    process is killed and `cut` is True. A non-zero exit, a timeout or a git that cannot start raises `NotFound`."""
    try:
        proc = subprocess.Popen(["git", *HARDENED, *args], cwd=repo, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise NotFound("git could not run") from exc
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        proc.kill()

    timer = threading.Timer(GIT_READ_TIMEOUT_S, expire)
    timer.start()
    cut = False
    try:
        if limit is None:
            out, _ = proc.communicate()
        else:
            assert proc.stdout is not None
            parts: list[bytes] = []
            got = 0
            while got <= limit:
                chunk = proc.stdout.read1(min(_CHUNK, limit + 1 - got))
                if not chunk:
                    break
                parts.append(chunk)
                got += len(chunk)
            out = b"".join(parts)
            if got > limit:
                cut = True
                out = out[:limit]
                proc.kill()
            proc.communicate()
    except (OSError, ValueError) as exc:
        proc.kill()
        proc.wait()
        raise NotFound("git failed") from exc
    finally:
        timer.cancel()
    if timed_out.is_set() or (not cut and proc.returncode != 0):
        raise NotFound("git failed")
    return out, cut


def _commits_files(repo: Path, shas: list[str]) -> dict[str, list[str]]:
    """`{sha: [path, ...]}` for the commits git still has, from one `git log --no-walk`; a commit git cannot read
    (the one call fails) falls back to one call per sha, dropping those that fail."""
    if not shas:
        return {}
    try:
        return _parse_log(_log(repo, shas))
    except NotFound:
        pass
    found: dict[str, list[str]] = {}
    for sha in shas:
        try:
            found.update(_parse_log(_log(repo, [sha])))
        except NotFound:
            continue
    return found


def _log(repo: Path, shas: list[str]) -> bytes:
    out, _ = _git(repo, "--literal-pathspecs", "log", "--no-walk=unsorted", "--no-show-signature",
                  "--format=%x01%H", "--name-only", "--no-renames", "-z", *shas, "--")
    return out


def _parse_log(out: bytes) -> dict[str, list[str]]:
    """Records split on `\\x01`: a sha, then NUL-separated paths (the first after a newline)."""
    found: dict[str, list[str]] = {}
    for record in out.split(b"\x01"):
        fields = record.split(b"\0")
        sha = fields[0].decode("ascii", errors="replace").strip()
        if not SHA.match(sha):
            continue
        paths: list[str] = []
        for i, raw in enumerate(fields[1:]):
            if i == 0 and raw.startswith(b"\n"):
                raw = raw[1:]
            if not raw:
                continue
            try:
                paths.append(raw.decode("utf-8"))
            except UnicodeDecodeError:
                continue  # a path the page could not name back is left out
        found[sha] = paths
    return found


# -- listing


def _cache_token(run: Run, repo: Path) -> tuple | None:
    path = run.events.path
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(Path(repo)), str(path), stat.st_size, stat.st_mtime_ns)


def run_artifacts(run: Run, repo: Path) -> list[dict]:
    """`[{path, phase, kind, revisions: [{sha, step, label, round, ts}]}]`, by phase order, then path."""
    token = _cache_token(run, repo)
    if token is not None and token in _CACHE:
        return copy.deepcopy(_CACHE[token])
    listing = _list(run.events.read(), Path(repo))
    if token is not None:
        while len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[token] = listing
    return copy.deepcopy(listing)


def _list(events: list[dict], repo: Path) -> list[dict]:
    phases: list[str] = []
    for ev in events:
        phase = ev.get("phase")
        if ev.get("type") == "phase_started" and ev.get("start_sha") and isinstance(phase, str) \
                and phase not in phases:
            phases.append(phase)
    rejected: dict[str, list[int]] = {}
    for ev in events:
        if ev.get("type") == "phase_rejected" and isinstance(ev.get("phase"), str):
            rejected.setdefault(ev["phase"], []).append(ev.get("seq", 0))
    commits: list[dict] = []
    for ev in sorted((e for e in events if e.get("type") == "step_committed"), key=lambda e: e.get("seq", 0)):
        key, sha = ev.get("key"), ev.get("sha")
        if not isinstance(key, str) or not isinstance(sha, str) or not SHA.match(sha):
            continue
        phase = key.split("/", 1)[0]
        if phase not in phases or "/" not in key:
            continue
        round_ = sum(1 for seq in rejected.get(phase, []) if seq < ev.get("seq", 0))
        last = key.rsplit("/", 1)[-1]
        commits.append({"phase": phase, "sha": sha, "step": key, "label": f"{last} r{round_}" if round_ > 0 else last,
                        "round": round_, "ts": ev.get("ts")})
    if not commits:
        return []
    files = _commits_files(repo, list(dict.fromkeys(c["sha"] for c in commits)))
    entries: dict[str, dict] = {}
    for commit in commits:
        for path in files.get(commit["sha"], []):
            entry = entries.get(path)
            if entry is None:
                entry = entries[path] = {"path": path, "phase": commit["phase"],
                                         "kind": PHASE_KINDS.get(commit["phase"], "file"), "revisions": []}
            if any(r["sha"] == commit["sha"] for r in entry["revisions"]):
                continue
            entry["revisions"].append({k: commit[k] for k in ("sha", "step", "label", "round", "ts")})
    order = {phase: i for i, phase in enumerate(phases)}
    return sorted(entries.values(), key=lambda e: (order[e["phase"]], e["path"]))


# -- reading


def _entry(run: Run, repo: Path, path: object) -> dict:
    if isinstance(path, str):
        for entry in run_artifacts(run, repo):
            if entry["path"] == path:
                return entry
    raise NotFound("that path is not an artifact of this run")  # never echo what a browser sent


def _lost(sha: str) -> NotFound:
    return NotFound(f"revision {sha[:7]} is no longer in git")


def _resolve(run: Run, repo: Path, entry: dict, sha: object) -> str:
    """The full sha of `sha`: one of `entry`'s revisions, or `head` (the run branch's tip). Else `NotFound`."""
    if sha == HEAD:
        branch = (run.state or {}).get("branch")
        if not isinstance(branch, str) or not branch or branch.startswith("-") or any(c.isspace() for c in branch):
            raise NotFound("this run has no branch")
        try:
            out, _ = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}")
        except NotFound:
            raise NotFound(f"branch {branch} is no longer in git") from None
        tip = out.decode("ascii", errors="replace").strip()
        if not SHA.match(tip):
            raise NotFound(f"branch {branch} is no longer in git")
        return tip
    if isinstance(sha, str) and SHA.match(sha):
        for revision in entry["revisions"]:
            if revision["sha"] == sha:
                return sha
    raise NotFound(f"that revision is not one of {entry['path']}")


def _check_commit(repo: Path, sha: str) -> None:
    try:
        _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
    except NotFound:
        raise _lost(sha) from None


def _cut(data: bytes, cut: bool) -> str:
    """`data` decoded; a cut one ends after its last newline, so no line shows half."""
    if cut and b"\n" in data:
        data = data[: data.rfind(b"\n") + 1]
    return data.decode("utf-8", errors="replace")


def _check_cap(max_bytes: object) -> int:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError(f"max_bytes must be a positive int, got {max_bytes!r}")
    return max_bytes


def read_artifact(run: Run, repo: Path, path: str, sha: str, max_bytes: int) -> dict:
    """`{path, sha, text, lines, truncated}`: `path` at revision `sha` (or `head`), cut at a line past `max_bytes`."""
    limit = _check_cap(max_bytes)
    repo = Path(repo)
    entry = _entry(run, repo, path)
    full = _resolve(run, repo, entry, sha)
    _check_commit(repo, full)
    try:
        out, _ = _git(repo, "--literal-pathspecs", "ls-tree", "-l", "-z", full, "--", entry["path"])
    except NotFound:
        raise _lost(full) from None
    blob = None
    for record in out.split(b"\0"):
        meta, tab, name = record.partition(b"\t")
        if not tab or name.decode("utf-8", errors="replace") != entry["path"]:
            continue
        fields = meta.split()
        if len(fields) == 4 and fields[0] in (b"100644", b"100755") and fields[1] == b"blob":
            blob = fields[2].decode("ascii", errors="replace")
            break
        raise NotFound(f"{entry['path']} is not a regular file at {full[:7]}")
    if blob is None:
        raise NotFound(f"{entry['path']} does not exist at {full[:7]}")
    if not SHA.match(blob):
        raise NotFound(f"{entry['path']} is not a regular file at {full[:7]}")
    try:
        data, cut = _git(repo, "cat-file", "blob", blob, limit=limit)
    except NotFound:
        raise _lost(full) from None
    text = _cut(data, cut)
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    return {"path": entry["path"], "sha": full, "text": text, "lines": lines, "truncated": cut}


def artifact_diff(run: Run, repo: Path, path: str, from_sha: str, to_sha: str, max_bytes: int) -> dict:
    """`{path, from, to, diff, truncated}`: `git diff` of `path` between two of its revisions, capped like a read."""
    limit = _check_cap(max_bytes)
    repo = Path(repo)
    entry = _entry(run, repo, path)
    old = _resolve(run, repo, entry, from_sha)
    new = _resolve(run, repo, entry, to_sha)
    _check_commit(repo, old)
    _check_commit(repo, new)
    try:
        data, cut = _git(repo, "--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--no-color",
                         "--no-renames", old, new, "--", entry["path"], limit=limit)
    except NotFound:
        raise _lost(new) from None
    return {"path": entry["path"], "from": old, "to": new, "diff": _cut(data, cut), "truncated": cut}
