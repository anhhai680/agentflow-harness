"""A run's artifacts, listed, read and diffed from the run's own commits in git, for the dashboard.

- `run_artifacts(run, repo)`: every file a phase's commits touched, by phase, each with its revisions (one per
  `step_committed` of that phase whose commit touched it). Cached per event log, like `views._METRICS_CACHE`.
- `read_artifact(run, repo, path, sha, max_bytes)`: one revision of one listed file, cut at a line past the cap.
- `artifact_diff(run, repo, path, from_sha, to_sha, max_bytes)`: `git diff` between two revisions of one file.
- `run_findings(run)`: the findings of each reviewed step and round (`step_findings`, `review_result`), newest open.
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

from agentflow.autonomy import as_finding, finding_text
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
_CACHE_LOCK = threading.Lock()  # the dashboard's request threads share the cache
_CHUNK = 64 << 10


class NotFound(LookupError):
    """What the dashboard answers 404 with; the message is the page's text."""


class _GitTimeout(NotFound):
    """Git timed out, could not start or could not be read: a passing failure, so a listing it hit is not cached."""


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
    process is killed and `cut` is True. A non-zero exit raises `NotFound`; a timeout or a git that cannot start or
    be read raises `_GitTimeout` (a `NotFound` too). stderr is discarded, so git can never stall on it, and the
    process is always reaped and its pipe closed, whatever ends the call."""
    try:
        proc = subprocess.Popen(["git", *HARDENED, *args], cwd=repo, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise _GitTimeout("git could not run") from exc
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        proc.kill()

    timer = threading.Timer(GIT_READ_TIMEOUT_S, expire)
    timer.start()
    cut = False
    try:
        assert proc.stdout is not None
        if limit is None:
            out = proc.stdout.read()
        else:
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
        proc.wait()
    except (OSError, ValueError) as exc:
        raise _GitTimeout("git could not be read") from exc
    finally:
        timer.cancel()
        if proc.returncode is None:
            proc.kill()
            proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()
    if timed_out.is_set():
        raise _GitTimeout("git timed out")
    if not cut and proc.returncode != 0:
        raise NotFound("git failed")
    return out, cut


def _commits_files(repo: Path, shas: list[str]) -> tuple[dict[str, list[str]], bool]:
    """`({sha: [path, ...]}, complete)` for the requested commits git still has, keyed by the requested sha.

    One `git log --no-walk=unsorted` first. Its records are trusted only when they come back exactly one per
    requested sha, in the requested order, with no record mark (`\\x01`) inside any path; otherwise (or when the call
    fails because a commit is gone) each sha gets its own call, which is parsed without splitting on that mark, and
    those that fail are dropped. `complete` is False when git timed out or could not run: the caller must not cache
    what it got. After a timeout no further call is made, so one listing waits at most one timeout."""
    if not shas:
        return {}, True
    try:
        batch = _parse_batch(_log(repo, shas), shas)
    except _GitTimeout:
        return {}, False
    except NotFound:
        batch = None
    if batch is not None:
        return batch, True
    found: dict[str, list[str]] = {}
    for sha in shas:
        try:
            paths = _parse_one(_log(repo, [sha]), sha)
        except _GitTimeout:
            return found, False
        except NotFound:
            continue
        if paths is not None:
            found[sha] = paths
    return found, True


def _log(repo: Path, shas: list[str]) -> bytes:
    out, _ = _git(repo, "--literal-pathspecs", "log", "--no-walk=unsorted", "--no-show-signature",
                  "--format=%x01%H", "--name-only", "--no-renames", "-z", *shas, "--")
    return out


def _paths(raw: bytes) -> list[str]:
    """The NUL-separated paths after a record's sha (the first after a newline); a non-UTF-8 one is left out."""
    if raw.startswith(b"\n"):
        raw = raw[1:]
    paths: list[str] = []
    for name in raw.split(b"\0"):
        if not name:
            continue
        try:
            paths.append(name.decode("utf-8"))
        except UnicodeDecodeError:
            continue  # a path the page could not name back is left out
    return paths


def _sha_of(head: bytes, requested: str) -> bool:
    full = head.decode("ascii", errors="replace")
    return bool(SHA.match(full)) and full.startswith(requested)


def _parse_one(out: bytes, sha: str) -> list[str] | None:
    """One commit's record: `\\x01<sha>\\0`, then its paths. Split on NUL only, never on the record mark."""
    if not out.startswith(b"\x01"):
        return None
    head, sep, rest = out[1:].partition(b"\0")
    if not sep or not _sha_of(head, sha):
        return None
    return _paths(rest)


def _parse_batch(out: bytes, shas: list[str]) -> dict[str, list[str]] | None:
    """The batch's records, or `None` when they are not exactly one per sha in order (a path held a record mark)."""
    if out.count(b"\x01") != len(shas):
        return None
    records = out.split(b"\x01")
    if records[0] != b"":
        return None
    found: dict[str, list[str]] = {}
    for sha, record in zip(shas, records[1:], strict=True):
        paths = _parse_one(b"\x01" + record, sha)
        if paths is None:
            return None
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
    """`[{path, phase, kind, revisions: [{sha, step, label, round, ts}]}]`, by phase order, then path.

    An event log that cannot be read lists nothing (`[]`, as a missing one does), so a read or diff of it is
    `NotFound`. A listing is cached only when its log was read and git answered every call."""
    token = _cache_token(run, repo)
    if token is not None:
        with _CACHE_LOCK:
            hit = _CACHE.get(token)
        if hit is not None:
            return copy.deepcopy(hit)
    try:
        events = run.events.read()
    except OSError:
        return []
    listing, complete = _list(events, Path(repo))
    if token is not None and complete:
        with _CACHE_LOCK:
            while len(_CACHE) >= _CACHE_MAX:
                oldest = next(iter(_CACHE), None)
                if oldest is None:
                    break
                _CACHE.pop(oldest, None)
            _CACHE[token] = listing
    return copy.deepcopy(listing)


def _list(events: list[dict], repo: Path) -> tuple[list[dict], bool]:
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
        return [], True
    files, complete = _commits_files(repo, list(dict.fromkeys(c["sha"] for c in commits)))
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
    return sorted(entries.values(), key=lambda e: (order[e["phase"]], e["path"])), complete


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


# -- findings

_DIGITS = re.compile(r"-?\d+")
_LINE_MAX = 999_999_999  # nine digits: past any file, and `int()` never sees a long string


def _line(value: object) -> int | None:
    """A line number: an int in 1.._LINE_MAX, or the first run of digits in a string (`"34-40"` is 34). A negative
    number (`"-5"`, `"L-5"`), a run of more than nine digits, or anything else is None. Never raises."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        found = _DIGITS.search(value)
        digits = found.group() if found else "-"
        value = int(digits) if not digits.startswith("-") and len(digits) <= len(str(_LINE_MAX)) else None
    return value if isinstance(value, int) and 0 < value <= _LINE_MAX else None


def _finding(raw: object) -> dict:
    finding = as_finding(raw)
    severity, file = finding.get("severity"), finding.get("file")
    return {"severity": severity.lower() if isinstance(severity, str) and severity else None,
            "file": file if isinstance(file, str) and file else None,
            "line": _line(finding.get("line")), "text": finding_text(finding)}


def run_findings(run: Run) -> list[dict]:
    """`[{key, label, round, ts, open, findings: [{severity, file, line, text}]}]`, in event order.

    One row per `step_findings` (its `round` is its 1-based position among that key's) and per `review_result`
    (its own `round`); `open` marks the newest row of each key. An event log that cannot be read lists nothing."""
    from agentflow.views import step_title  # views imports this module, so it is imported here, not at the top

    try:
        events = run.events.read()
    except OSError:
        return []
    rows: list[dict] = []
    counts: dict[str, int] = {}
    for ev in sorted(events, key=lambda e: e.get("seq", 0)):
        key, kind = ev.get("key"), ev.get("type")
        if kind not in ("step_findings", "review_result") or not isinstance(key, str):
            continue
        if kind == "step_findings":
            counts[key] = counts.get(key, 0) + 1
            round_ = counts[key]
        else:
            round_ = ev.get("round") if isinstance(ev.get("round"), int) else 0
        found = ev.get("findings")
        rows.append({"key": key, "label": step_title(key), "round": round_, "ts": ev.get("ts"), "open": False,
                     "findings": [_finding(f) for f in found] if isinstance(found, list) else []})
    newest: dict[str, dict] = {row["key"]: row for row in rows}
    for row in newest.values():
        row["open"] = True
    return rows
