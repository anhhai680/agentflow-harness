"""Claims on backlog items: one ref per item on `origin`, created and moved with leased pushes.

Item `<id>`'s claim is `refs/agentflow/claims/<id>` (or `refs/heads/agentflow/claims/<id>` with
`backlog.claim_ref: branch`). Its commit holds one file, `claim.json`, with the keys `item`, `run`, `lane`, `phase`,
`status`, `by`, `started` and `updated`. Claiming pushes with an empty lease (the ref must not exist yet), and the
server checks it under the ref's lock, so of two simultaneous claims exactly one wins. Moving or deleting a claim is
leased on the sha the claimant holds, so a claim someone else deleted or replaced is never pushed over.

A claim is data anyone with push access can write. Every field is validated (`parse_claim`); anything else is an
*unreadable* claim, which still blocks a start but is shown only by its reason. Ids go through `sources.MODULE_ID`
before they reach a ref name or an argv. Every git call runs through `_run`: `HARDENED` (no hook, no fsmonitor, no
replace object), no prompt, no stdin and a 60 s timeout. Every push is leased, never forced, with an explicit refspec
and `--no-verify`. This module knows nothing of runs.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from agentflow import gitops
from agentflow.sources import MODULE_ID

if TYPE_CHECKING:
    from agentflow.config import Config

STATUSES = ("running", "waiting", "paused", "stopped")
FIELDS = ("item", "run", "lane", "phase", "status", "by", "started", "updated")
CLAIM_FILE = "claim.json"
CLAIM_MAX_BYTES = 4096  # a larger `claim.json` is unreadable, never read
BY_MAX = 100
LIST_MAX = 1000  # claim refs `list_remote` reads at most; past it, the first by name
LOCAL_PREFIX = "refs/agentflow/remote-claims/"  # where fetched claims (and the commits this clone pushed) are kept
HELD_PREFIX = "refs/agentflow/held-claims/"  # the last commit this clone pushed of each claim: no fetch overwrites it
GIT_TIMEOUT_S = 60
EMAIL = "agentflow@localhost"
_PREFIX = {"refs": "refs/agentflow/claims/", "branch": "refs/heads/agentflow/claims/"}
_FULL_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")
_TIME_MAX = 40
_DETAIL_MAX = 300
# The variables that would point git at another repository than `repo`.
_GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR", "GIT_NAMESPACE")


@dataclass(frozen=True)
class Claim:
    item: str
    run: str
    lane: str
    phase: str
    status: str
    by: str
    started: str
    updated: str
    sha: str = ""
    unreadable: str = ""  # why `claim.json` could not be read; "" when it could


class ClaimError(Exception):
    """A claim could not be made, moved or read."""


class Claimed(ClaimError):
    """The item already has a claim on `origin` (possibly an unreadable one)."""

    def __init__(self, claim: Claim) -> None:
        self.claim = claim
        super().__init__(describe(claim))


class HostRefused(ClaimError):
    """`origin` refused to create the claim ref twice, and the ref does not exist: a host that refuses custom refs,
    or a push refused for another reason (credentials, read-only access). `detail` is git's last line, one printable
    line cut short; the `backlog.claim_ref: branch` advice is a maybe, and never given to a pack already on `branch`."""

    def __init__(self, name: str, detail: str, form: str) -> None:
        self.name, self.detail = name, one_line(detail) or "git gave no reason"
        advice = "; if origin refuses custom refs, backlog.claim_ref: branch is the fallback" if form == "refs" else ""
        super().__init__(f"origin refused the claim ref {name}: {self.detail}{advice}")


class Unreachable(ClaimError):
    """`origin` could not be reached, or did not take a push for a reason other than the lease."""


class Lost(ClaimError):
    """The held claim is gone (`claim` is None) or another claim holds the ref (`claim`)."""

    def __init__(self, claim: Claim | None, item: str = "") -> None:
        self.claim = claim
        if claim is None:
            text = f"the claim on {item or 'the item'} is gone from origin"
        else:
            text = f"the claim on {claim.item} was taken: {describe(claim)}"
        super().__init__(text)


def describe(claim: Claim) -> str:
    """One line naming who holds `claim`, built only from validated fields, each cut to one printable line."""
    if claim.unreadable:
        return f"{one_line(claim.item)} has an unreadable claim on origin ({one_line(claim.unreadable)})"
    by, run, phase, status, updated = (one_line(getattr(claim, key))
                                       for key in ("by", "run", "phase", "status", "updated"))
    return f"{one_line(claim.item)} is claimed by {by} (run {run}, {phase}, {status}, updated {updated})"


# --------------------------------------------------------------------------- git


def _run(repo: Path, *args: str, input: str | None = None, env: Mapping[str, str] | None = None
         ) -> subprocess.CompletedProcess:
    """`git <HARDENED> <args>` in `repo`: no prompt, no stdin unless `input`, a 60 s timeout. A timeout or a git
    that can't start comes back as a failed process (exit 124 or 127), never as an exception."""
    full = {**{k: v for k, v in os.environ.items() if k not in _GIT_ENV}, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    argv = ["git", *gitops.HARDENED, *args]
    extra = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    try:
        return subprocess.run(argv, cwd=Path(repo), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=GIT_TIMEOUT_S, env=full, check=False, **extra)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"git {args[0] if args else ''} timed out")
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"could not run git: {exc}")


def _detail(proc: subprocess.CompletedProcess) -> str:
    """The last line git wrote, printable and cut short: it may come from the remote."""
    lines = [ln.strip() for ln in f"{proc.stderr or ''}\n{proc.stdout or ''}".splitlines() if ln.strip()]
    line = lines[-1] if lines else f"git exited {proc.returncode}"
    return "".join(c if c.isprintable() else "?" for c in line)[:_DETAIL_MAX]


def _ok(proc: subprocess.CompletedProcess, what: str) -> str:
    if proc.returncode != 0:
        raise ClaimError(f"could not {what}: {_detail(proc)}")
    return proc.stdout.strip()


# --------------------------------------------------------------------------- names


def _check_id(item_id: object) -> str:
    if not isinstance(item_id, str) or MODULE_ID.fullmatch(item_id) is None:
        raise ValueError(f"not a module id: {item_id!r}")
    return item_id


def prefix(cfg: Config) -> str:
    """The ref prefix every claim of this pack's `backlog.claim_ref` form lives under (ends in `/`)."""
    return _PREFIX[cfg.backlog.claim_ref]


def ref(cfg: Config, item_id: str) -> str:
    """The claim ref of `item_id` on `origin`; `ValueError` for an id that is not a module id."""
    return _PREFIX[cfg.backlog.claim_ref] + _check_id(item_id)


def _local(item_id: str) -> str:
    return LOCAL_PREFIX + _check_id(item_id)


def _held_sha(held: Claim) -> str:
    if not isinstance(held.sha, str) or _FULL_SHA.fullmatch(held.sha) is None:
        raise ValueError(f"not a claim this clone holds: {held.sha!r}")
    return held.sha


def enabled(cfg: Config, repo: Path) -> bool:
    """Claims apply: a `backlog` pack, `backlog.claims` on, and an `origin` remote."""
    source = cfg.work_source
    if source is None or source.kind != "backlog" or not cfg.backlog.claims:
        return False
    proc = _run(repo, "config", "--get", "remote.origin.url")
    return proc.returncode == 0 and bool(proc.stdout.strip())


def committer(repo: Path) -> str:
    """`git config user.name` as one printable line of at most 100 characters, else `unknown`."""
    proc = _run(repo, "config", "--get", "user.name")
    name = proc.stdout if proc.returncode == 0 else ""
    one = " ".join("".join(c if c.isprintable() else " " for c in name).split())
    return one[:BY_MAX].strip() or "unknown"


# --------------------------------------------------------------------------- the claim's text


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(text: object) -> datetime | None:
    """An ISO-8601 time with a zone, or None. Printable only: `fromisoformat` takes any one character as the date and
    time separator, a newline or an escape among them."""
    if (not isinstance(text, str) or not 0 < len(text) <= _TIME_MAX or not text.isascii()
            or not text.isprintable()):
        return None
    try:
        found = datetime.fromisoformat(text)
    except ValueError:
        return None
    return found if found.tzinfo is not None else None


def _by_ok(by: object) -> bool:
    return isinstance(by, str) and 0 < len(by) <= BY_MAX and by.isprintable() and by == by.strip()


def _why_not(cfg: Config, item_id: str, data: object) -> str:
    """Why `data` is not a valid claim on `item_id`; "" when it is."""
    if not isinstance(data, dict):
        return "claim.json is not an object"
    missing = [key for key in FIELDS if key not in data]
    if missing:
        return f"claim.json lacks {', '.join(missing)}"
    wrong = [key for key in FIELDS if not isinstance(data[key], str)]
    if wrong:
        return f"claim.json has a non-text {', '.join(wrong)}"
    if data["item"] != item_id:
        return "claim.json names another item"
    if not _RUN_ID.fullmatch(data["run"]):
        return "claim.json has an invalid run"
    if data["lane"] not in cfg.lanes:
        return "claim.json names a lane this pack lacks"
    if data["phase"] not in cfg.phases:
        return "claim.json names a phase this pack lacks"
    if data["status"] not in STATUSES:
        return "claim.json has an invalid status"
    if not _by_ok(data["by"]):
        return "claim.json has an invalid by"
    if _when(data["started"]) is None or _when(data["updated"]) is None:
        return "claim.json has an invalid time"
    return ""


def _unreadable(item_id: str, sha: str, why: str) -> Claim:
    return Claim(item=item_id, run="", lane="", phase="", status="", by="", started="", updated="",
                 sha=sha, unreadable=why)


def parse_claim(cfg: Config, item_id: str, text: str, sha: str) -> Claim:
    """The claim `text` (a `claim.json`) states for `item_id`, at commit `sha`. Never raises: text that is not a
    valid claim on `item_id` comes back unreadable, with the reason and `sha`, so it still blocks a start."""
    if not isinstance(text, str) or len(text.encode("utf-8", errors="surrogatepass")) > CLAIM_MAX_BYTES:
        return _unreadable(item_id, sha, f"claim.json is not text of at most {CLAIM_MAX_BYTES} bytes")
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return _unreadable(item_id, sha, "claim.json is not JSON")
    why = _why_not(cfg, item_id, data)
    if why:
        return _unreadable(item_id, sha, why)
    return Claim(**{key: data[key] for key in FIELDS}, sha=sha)


def _text(claim: Claim) -> str:
    return json.dumps({key: getattr(claim, key) for key in FIELDS}) + "\n"


def _ident(by: str) -> str:
    """`by` as a git ident name: git refuses `<`, `>` and a newline in one."""
    name = " ".join("".join(c if c.isprintable() and c not in "<>" else " " for c in by).split())
    return name.strip(".,:;'\"\\ ") or "unknown"


def _commit(repo: Path, claim: Claim, parent: str | None) -> str:
    """The commit of `claim` (one file, `claim.json`), on `parent` when given, written without a work tree. Its
    message carries a random nonce, so two commits are never equal: an equal sha on `origin` proves authorship."""
    blob = _ok(_run(repo, "hash-object", "-w", "--no-filters", "--stdin", input=_text(claim)), "write the claim")
    if not _FULL_SHA.fullmatch(blob):
        raise ClaimError(f"could not write the claim: git returned {blob[:80]!r}")
    tree = _ok(_run(repo, "mktree", input=f"100644 blob {blob}\t{CLAIM_FILE}\n"), "write the claim's tree")
    if not _FULL_SHA.fullmatch(tree):
        raise ClaimError(f"could not write the claim's tree: git returned {tree[:80]!r}")
    name = _ident(claim.by)
    env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": EMAIL, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": EMAIL}
    parents = ("-p", parent) if parent else ()
    sha = _ok(_run(repo, "-c", "commit.gpgsign=false", "commit-tree", tree, *parents, "-m",
                   f"claim {claim.item} {secrets.token_hex(8)}",
                   env=env), "commit the claim")
    if not _FULL_SHA.fullmatch(sha):
        raise ClaimError(f"could not commit the claim: git returned {sha[:80]!r}")
    return sha


def _keep(repo: Path, item_id: str, sha: str) -> None:
    """Point the local copy of `item_id`'s claim, and the ref of the claim this clone holds (which no fetch moves), at
    `sha`, so the commit this clone pushed is never pruned while it holds it."""
    _run(repo, "update-ref", _local(item_id), sha)
    _run(repo, "update-ref", HELD_PREFIX + _check_id(item_id), sha)


# --------------------------------------------------------------------------- origin


def _push(repo: Path, name: str, expected: str, source: str) -> subprocess.CompletedProcess:
    """Push `source` (a sha, or "" to delete) to `name` on `origin`, leased on `expected` ("": must not exist)."""
    return _run(repo, "push", "--no-verify", "--no-signed", "--recurse-submodules=no",
                f"--force-with-lease={name}:{expected}", "origin", f"{source}:{name}")


def _ls_remote(repo: Path, *patterns: str) -> dict[str, str]:
    """`{ref: sha}` on `origin` of the refs `patterns` match; `Unreachable` when `origin` can't be read."""
    proc = _run(repo, "ls-remote", "--refs", "origin", *patterns)
    if proc.returncode != 0:
        raise Unreachable(f"could not reach origin: {_detail(proc)}")
    found: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        sha, sep, name = line.partition("\t")
        if sep and _FULL_SHA.fullmatch(sha):
            found[name] = sha
    return found


def _fetch(repo: Path, wanted: Mapping[str, str]) -> dict[str, str] | None:
    """Fetch `{item_id: remote ref}` into `refs/agentflow/remote-claims/<id>` with one `fetch`; `{item_id: sha}` of
    what arrived, or None when the fetch failed."""
    if not wanted:
        return {}
    specs = [f"+{name}:{_local(item_id)}" for item_id, name in wanted.items()]
    proc = _run(repo, "-c", "gc.auto=0", "-c", "maintenance.auto=false", "fetch", "-q", "--no-tags",
                "--no-write-fetch-head", "--recurse-submodules=no", "origin", *specs)
    if proc.returncode != 0:
        return None
    listed = _run(repo, "for-each-ref", "--format=%(objectname) %(refname)", LOCAL_PREFIX)
    if listed.returncode != 0:
        return None
    arrived: dict[str, str] = {}
    for line in listed.stdout.splitlines():
        sha, _, name = line.partition(" ")
        item_id = name.removeprefix(LOCAL_PREFIX)
        if item_id in wanted and _FULL_SHA.fullmatch(sha):
            arrived[item_id] = sha
    return arrived if set(arrived) == set(wanted) else None


def _load(cfg: Config, repo: Path, item_id: str, sha: str) -> Claim:
    """The claim commit `sha` (already fetched) holds for `item_id`."""
    data = gitops.read_blob(repo, sha, CLAIM_FILE, CLAIM_MAX_BYTES)
    if data is None:
        return _unreadable(item_id, sha, f"no {CLAIM_FILE} of at most {CLAIM_MAX_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return _unreadable(item_id, sha, f"{CLAIM_FILE} is not UTF-8")
    return parse_claim(cfg, item_id, text, sha)


def read(cfg: Config, repo: Path, item_id: str) -> Claim | None:
    """The claim on `item_id` on `origin` now, or None when there is none; `Unreachable` when `origin` can't be
    read."""
    name = ref(cfg, item_id)
    for _ in range(2):  # a claim released between the listing and the fetch is listed again
        sha = _ls_remote(repo, name).get(name)
        if sha is None:
            return None
        arrived = _fetch(repo, {item_id: name})
        if arrived is not None:
            return _load(cfg, repo, item_id, arrived[item_id])
    raise Unreachable(f"could not fetch {name} from origin")


def list_remote(cfg: Config, repo: Path, seen: Mapping[str, Claim] | None = None) -> dict[str, Claim]:
    """Every claim on `origin`, by item id: one `ls-remote`, then one `fetch` of only the refs whose sha differs
    from `seen`'s (the claims a previous call returned). A ref whose name is not a module id is left out.
    `Unreachable` when `origin` can't be read."""
    prefix = _PREFIX[cfg.backlog.claim_ref]
    seen = seen or {}
    for _ in range(2):  # a claim released between the listing and the fetch is listed again
        listed: dict[str, str] = {}
        for name, sha in sorted(_ls_remote(repo, f"{prefix}*").items()):
            item_id = name.removeprefix(prefix)
            if name.startswith(prefix) and MODULE_ID.fullmatch(item_id) and len(listed) < LIST_MAX:
                listed[item_id] = sha
        kept = {i: seen[i] for i, sha in listed.items() if i in seen and seen[i].sha == sha}
        wanted = {i: prefix + i for i in listed if i not in kept}
        arrived = _fetch(repo, wanted)
        if arrived is not None:
            fetched = {i: _load(cfg, repo, i, sha) for i, sha in arrived.items()}
            return {i: kept[i] if i in kept else fetched[i] for i in listed}
    raise Unreachable("could not fetch the claims from origin")


OFF = "claims are off: {why}"
UNREAD = "could not read claims on origin: {error}"


def one_line(text: object, limit: int = _DETAIL_MAX) -> str:
    """`text` as one printable line of at most `limit` characters: for text from `origin` or a cache."""
    return " ".join("".join(c if c.isprintable() else " " for c in str(text)).split())[:limit]


def survey(cfg: Config, repo: Path, seen: Mapping[str, Claim] | None = None) -> tuple[dict[str, Claim] | None, str]:
    """`(claims, note)`: every claim on `origin` now (`list_remote`), and the note a Board or a queue shows. Outside
    a `backlog` pack `({}, "")`; with claims off, or no `origin` remote, `({}, "claims are off: <why>")`; when
    `origin` can't be read `(None, "could not read claims on origin: <error>")`. Never raises."""
    source = cfg.work_source
    if source is None or source.kind != "backlog":
        return {}, ""
    if not cfg.backlog.claims:
        return {}, OFF.format(why="backlog.claims is false")
    try:
        if not enabled(cfg, repo):
            return {}, OFF.format(why="this clone has no origin remote")
        return list_remote(cfg, repo, seen), ""
    except Exception as exc:  # noqa: BLE001 - a Board refresh or a queue tick must not die on origin
        return None, one_line(UNREAD.format(error=one_line(exc) or type(exc).__name__))


def restore(cfg: Config, item_id: object, record: object) -> Claim | None:
    """The claim a cache kept for `item_id` (`dataclasses.asdict(Claim)`), checked again as `parse_claim` checks a
    `claim.json`: None when `item_id` is not a module id, the sha is not a full sha, a field is not text, or the
    claim is not one this pack can read. An unreadable claim keeps its reason as one capped line. Never raises: the
    cache is a file on disk, not trusted."""
    if not isinstance(item_id, str) or MODULE_ID.fullmatch(item_id) is None or not isinstance(record, dict):
        return None
    sha, why = record.get("sha"), record.get("unreadable")
    if not isinstance(sha, str) or _FULL_SHA.fullmatch(sha) is None or not isinstance(why, str):
        return None
    if why:
        return _unreadable(item_id, sha, one_line(why) or "unreadable")
    try:
        text = json.dumps({key: record.get(key) for key in FIELDS})
    except (TypeError, ValueError, RecursionError):
        return None
    found = parse_claim(cfg, item_id, text, sha)
    return None if found.unreadable else found


# --------------------------------------------------------------------------- the protocol


def _check_fields(cfg: Config, run: str, lane: str, phase: str, status: str) -> None:
    if not isinstance(run, str) or not _RUN_ID.fullmatch(run):
        raise ValueError(f"not a run id: {run!r}")
    if lane not in cfg.lanes:
        raise ValueError(f"not a lane of this pack: {lane!r}")
    if phase not in cfg.phases:
        raise ValueError(f"not a phase of this pack: {phase!r}")
    if status not in STATUSES:
        raise ValueError(f"not a claim status: {status!r}")


def create(cfg: Config, repo: Path, item_id: str, *, run: str, lane: str, phase: str,
           status: str = "running") -> Claim:
    """Claim `item_id` on `origin` for `run`: push a new claim commit leased on the ref not existing.

    On a failed push one `ls-remote` decides: the ref holds this very commit → success (the push reached `origin` and
    only its answer was lost); it holds another → `Claimed` (with the claim, read); it doesn't exist → push once more
    (a claim released between the push and the listing, or a passing failure), and `HostRefused` (with git's message)
    when the second push fails too and the ref still doesn't exist; `origin` can't be read → `Unreachable`."""
    name = ref(cfg, item_id)
    _check_fields(cfg, run, lane, phase, status)
    now = _now()
    claim = Claim(item=item_id, run=run, lane=lane, phase=phase, status=status, by=committer(repo),
                  started=now, updated=now)
    sha = _commit(repo, claim, None)
    for attempt in range(2):  # a claim released while it was being pushed or read is tried once more
        proc = _push(repo, name, "", sha)
        listed = _ls_remote(repo, name) if proc.returncode != 0 else {}
        if proc.returncode == 0 or listed.get(name) == sha:
            _keep(repo, item_id, sha)
            return replace(claim, sha=sha)
        if name not in listed:
            if attempt == 0:
                continue
            raise HostRefused(name, _detail(proc), cfg.backlog.claim_ref)
        current = read(cfg, repo, item_id)
        if current is not None and current.sha == sha:
            _keep(repo, item_id, sha)
            return replace(claim, sha=sha)
        if current is not None:
            raise Claimed(current)
    raise Unreachable(f"the claim ref {name} kept changing on origin")


def _descends(repo: Path, ancestor: str, sha: str) -> bool:
    """`sha` is `ancestor` or a commit built on it (both already in `repo`)."""
    if not (_FULL_SHA.fullmatch(ancestor) and _FULL_SHA.fullmatch(sha)):
        return False
    return _run(repo, "merge-base", "--is-ancestor", ancestor, sha).returncode == 0


def held(cfg: Config, repo: Path, item_id: str, sha: str, run: str) -> Claim:
    """The claim commit `sha` that this clone made for `run` on `item_id`, read from `repo`'s own objects (no
    network): what `advance` takes. `ClaimError` when it is not a readable claim of `run` here."""
    _check_id(item_id)
    if not isinstance(sha, str) or _FULL_SHA.fullmatch(sha) is None:
        raise ClaimError(f"not a claim this clone holds: {str(sha)[:80]!r}")
    found = _load(cfg, repo, item_id, sha)
    if found.unreadable:
        raise ClaimError(f"the claim {sha[:12]} on {item_id} can't be read here: {found.unreadable}")
    if found.run != run:
        raise ClaimError(f"the claim {sha[:12]} on {item_id} is not run {run}'s")
    return found


def own(repo: Path, held_sha: str, run: str, claim: Claim | None) -> bool:
    """`claim` (one `Lost` carries) is a newer claim of the same run: the run id `run`, on a commit built on
    `held_sha`, as another process of the run makes it. Another machine's claim starts from a root commit."""
    return (claim is not None and not claim.unreadable and claim.run == run
            and _descends(repo, held_sha, claim.sha))


def _settle(cfg: Config, repo: Path, held: Claim, expected: str, proc: subprocess.CompletedProcess,
            retried: bool) -> Claim | None:
    """After a leased push on `expected` failed: the claim of the same run to retry on, or None when the ref is
    gone. The same run means the same run id *and* a commit that descends from `held.sha`: run ids are not unique
    across machines, but another process of this run builds on this run's own commits, while another machine's
    claim starts from a root commit. `Lost` when another claim holds the ref, or when the same run moved it again
    after the one retry (the retry is exhausted, and `Lost` carries the run's own claim); `Unreachable` when
    `origin` can't be read, or still holds `expected` (the push failed for another reason than its lease)."""
    current = read(cfg, repo, held.item)
    if current is None:
        return None
    if current.unreadable or current.run != held.run:
        raise Lost(current, held.item)
    if current.sha == expected:
        raise Unreachable(f"could not push the claim on {held.item}: {_detail(proc)}")
    if not _descends(repo, held.sha, current.sha) or retried:
        raise Lost(current, held.item)
    return current


def advance(cfg: Config, repo: Path, held: Claim, *, phase: str, status: str) -> Claim:
    """Move the claim `held` to `phase` and `status`: a new commit on top, pushed leased on `held.sha`. When the ref
    holds a newer commit of the same run (the same run id, built on `held.sha`: another process of it), retry once
    on that. `Lost` when the claim is gone, another claim holds the ref (nothing is pushed over it), or the same run
    moved it again after the retry (`Lost` then carries the run's own claim); `Unreachable` when `origin` can't be
    reached."""
    name = ref(cfg, held.item)
    expected = _held_sha(held)
    _check_fields(cfg, held.run, held.lane, phase, status)
    base = held
    for retried in (False, True):
        claim = replace(base, phase=phase, status=status, updated=_now(), sha="", unreadable="")
        sha = _commit(repo, claim, expected)
        proc = _push(repo, name, expected, sha)
        if proc.returncode == 0:
            _keep(repo, held.item, sha)
            return replace(claim, sha=sha)
        current = _settle(cfg, repo, held, expected, proc, retried)
        if current is None:
            raise Lost(None, held.item)
        base, expected = replace(held, started=current.started), current.sha
    raise AssertionError("unreachable")  # pragma: no cover


def release(cfg: Config, repo: Path, held: Claim) -> None:
    """Delete the claim `held` from `origin`, leased on `held.sha`. A ref already gone is success; one holding a
    newer commit of the same run (the same run id, built on `held.sha`) is deleted on that, once. `Lost` when
    another claim holds the ref (it stays), or the same run moved it again after the retry (`Lost` then carries the
    run's own claim); `Unreachable` when `origin` can't be reached."""
    name = ref(cfg, held.item)
    expected = _held_sha(held)
    for retried in (False, True):
        proc = _push(repo, name, expected, "")
        if proc.returncode == 0:
            break
        current = _settle(cfg, repo, held, expected, proc, retried)
        if current is None:
            break
        expected = current.sha
    _run(repo, "update-ref", "-d", _local(held.item))
    _run(repo, "update-ref", "-d", HELD_PREFIX + held.item)


def release_item(cfg: Config, repo: Path, item_id: str) -> Claim | None:
    """A person frees the claim on `item_id`, whoever holds it: read it, then delete it leased on the sha just read
    (`release`), so a claim made between the read and the delete stays. Works on an unreadable claim too, whose sha is
    known. The claim freed, or None when there was none. `ValueError` for an id that is not a module id; `Lost` when
    another claim took the ref meanwhile; `Unreachable` when `origin` can't be reached."""
    found = read(cfg, repo, item_id)
    if found is None:
        return None
    release(cfg, repo, found)
    return found


# --------------------------------------------------------------------------- age


def age_s(held: Claim, now: datetime) -> float | None:
    """Seconds since `held` was last updated, or None when its `updated` is not a time."""
    updated = _when(held.updated)
    if updated is None:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return (now - updated).total_seconds()


def is_stale(held: Claim, hours: int, now: datetime) -> bool:
    """`held` was not updated for more than `hours`, whatever its status."""
    age = age_s(held, now)
    return age is not None and age > hours * 3600
