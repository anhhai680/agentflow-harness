"""Build the knowledge graph of one commit as a delta over the last snapshot, parsing only the blobs that changed.

`build` indexes the base branch's commit (`origin/<base_branch>`, else the local branch) or the commit it is given.
It holds `<graph dir>/build.lock` before it touches the store (`graph_store.begin` removes every temp file beside the
store, so a second builder must be refused first), copies the published store into a temp file, applies what changed
and swaps the copy in with `graph_store.publish`. Any failure discards the copy, so the old snapshot stays readable;
an expected one is a `BuildError` with a one-line message, never a traceback.

The steps, in order:

1. **The change list.** `git diff-tree` between the stored commit and the new one. When git cannot diff them (the old
   commit was rewritten and pruned), the new tree is compared with the stored rows by blob id and mode. A first
   build, a `full` one (a repair from a new schema, keeping nothing) and one whose settings (`graph.exclude`,
   `graph.max_file_kb`, the doc roots, the repository id, the engine version, whose selection and resolution rules
   may differ) differ from the last build's take every listed path.
2. **`.gitignore`.** When a commit changes any ignore file, the tree's ignore files are taken shallowest first, and
   one that the shallower files ignore (`generated/.gitignore` under a `generated/` pattern) is never read, as git
   never loads it; the others are read from their blobs (or kept from the store when unchanged). When the set of
   texts moved, every listed path is judged again and those whose state moved join the change list; otherwise only
   the changed paths are judged. A path that is not `indexed` (ignored, secret, vendored, excluded, a symlink or a
   submodule) is never read.
3. **Facts.** A changed blob's facts come from the cache by blob id, language and extractor version, else from the
   parser worker, which runs in a child process; nothing in the repository is ever executed.
4. **Resolution.** Every changed path, and every path that wants a key a changed path provided before or provides now,
   is resolved again from stored facts. An affected path is never re-read, whatever its key set did.

A blob git fails to give (`graph_git.ReadFailed`: git failed or timed out, or the object is not in the store) is a
`BuildError`, never data: storing it as "not read" would be a verdict no later build revisits, since the blob id
does not change. A full disk or a failed `COMMIT` (`sqlite3.OperationalError`, `OSError`) is a `BuildError` too.

**When the parser starts, and what is extracted again.** Each `files` row records the extractor version its facts
came from (`""` when no extractor gave a verdict: `no_grammar`, `parser_unavailable`), and `meta.languages` records
the versions the last build's parser offered (trusted only for the same engine version and where its extractor part
equals `EXTRACTOR_VERSIONS`). The parser starts when a changed blob has no cached facts under those versions, or when
a *parse candidate* (an indexed row, or an unparsed one the parser failed on) records another version than that map
for its language: a language the last parser did not offer, a grammar that came later, may be offered now. Once it
runs, every candidate whose language it offers at another version is extracted again; a language it does not offer
keeps its stored facts. On a machine that lacks a grammar, a repository with files in that language so starts the
parser once per build to look again, and parses nothing when the grammar is still missing.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import itertools
import posixpath
import sqlite3
import stat
import time
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agentflow import graph_git, graph_parse, graph_store, pack, safeio
from agentflow.graph_git import Entry
from agentflow.graph_ignore import MAX_FILE_BYTES as IGNORE_MAX_BYTES
from agentflow.graph_ignore import Ignore
from agentflow.graph_model import EXTRACTOR_VERSIONS, NOTE_MAX, Facts, empty
from agentflow.graph_resolve import file_node, provided, resolve, wanted
from agentflow.graph_select import parses, state
from agentflow.graph_store import FileRow

if TYPE_CHECKING:
    from agentflow.config import Config

LOCK = "build.lock"
NOTES_MAX = 50
SNIFF_BYTES = 8000  # a NUL among a file's first bytes makes it binary: a file node, never parsed
TOO_BIG, BINARY = "over_max_file_kb", "binary"  # the notes of a file over `graph.max_file_kb` and of a binary one
_HAS_NODES = ("indexed", "unparsed")
_REGULAR = ("100644", "100755")
_NO_VERDICT = frozenset({"no_grammar", "parser_unavailable"})  # stored with version "": tried again by a later parser
_PARSE_FAILED = _NO_VERDICT | {"too_large", "parser_timeout", "parser_memory", "parser_crashed", "parser_reply"}
_NOT_PATHS = ("spec_file", "done_status")  # `project.docs` keys whose values are a pattern, not a path


class BuildError(Exception):
    """A build that cannot run or finish for an expected reason; its message is one line for a person."""


@dataclass(frozen=True)
class Result:
    commit: str
    previous: str | None  # the commit the store was built at before, None for a first or `full` build
    full: bool  # every listed path was handled, not only the changed ones
    parsed: int  # blobs handed to the parser
    cached: int  # blobs whose facts came from the cache
    resolved: int  # paths whose nodes and edges were made again
    removed: int  # paths that had nodes and have none now
    left_out: dict[str, int]  # `files` rows by state, for every state but `indexed` and `unparsed`
    languages: dict[str, str]  # each language a file with nodes has: "exact" or "not parsed"
    notes: tuple[str, ...]
    ms: int


def doc_roots(docs: Mapping[str, object]) -> tuple[str, ...]:
    """The folders `project.docs` names: each value's directory part (its folder when it names a file)."""
    roots: set[str] = set()
    for key, value in docs.items():
        if key in _NOT_PATHS or not isinstance(value, str) or "{" in value:
            continue
        text = value.strip().strip("/")
        path = posixpath.normpath(text) if text else ""
        if "." in posixpath.basename(path):
            path = posixpath.dirname(path)
        if path and path != "." and not path.startswith(".."):
            roots.add(path)
    return tuple(sorted(roots))


def _target(repo: Path, base_branch: str, commit: str | None) -> str:
    if commit is not None:
        if not graph_git.is_commit(repo, commit):
            raise BuildError(f"{commit!r} is not the full id of a commit in this repository")
        return commit
    found = graph_git.base_commit(repo, base_branch)
    if found is None:
        raise BuildError(f"no commit to index: neither origin/{base_branch} nor {base_branch} names one")
    return found


@contextlib.contextmanager
def _locked(directory: Path) -> Iterator[None]:
    """Hold `<directory>/build.lock` (made `0700` and `0600`); `BuildError` at once when another build holds it."""
    try:
        if directory.is_symlink():
            raise safeio.SymlinkRefused(f"refusing to use a symlink at {directory}")
        safeio.ensure_dir(directory.parent)
        with contextlib.suppress(FileExistsError):
            os.mkdir(directory, 0o700)
        if directory.is_symlink() or not directory.is_dir():
            raise safeio.SymlinkRefused(f"{directory} is not a directory")
        fd = os.open(directory / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError as exc:
        raise BuildError(f"cannot lock the graph directory {directory}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BuildError(f"{directory / LOCK} is not a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BuildError("another build is running") from None
        except OSError as exc:  # a filesystem that cannot lock (`ENOLCK`, `EOPNOTSUPP`)
            raise BuildError(f"cannot lock {directory / LOCK}: {exc}") from exc
        yield
    finally:
        os.close(fd)


def build(repo: Path, cfg: Config, *, full: bool = False, download: bool = True, commit: str | None = None) -> Result:
    """Index `commit` (default: the base branch's) into the repository's store; see the module docstring."""
    started = time.monotonic()
    if not cfg.graph.enabled:
        raise BuildError("graph.enabled is false: set it to true in .agentflow/config.yaml to build the index")
    if not graph_store.fts5_available():
        raise BuildError("this Python's SQLite has no FTS5: use a Python whose SQLite has FTS5")
    repo = Path(repo)
    target = _target(repo, cfg.project.base_branch, commit)
    store = graph_store.path_for(pack.state_dir(repo))
    with _locked(store.parent):
        try:
            conn, temp = graph_store.begin(store, fresh=full)
        except (graph_store.Unusable, OSError) as exc:
            raise BuildError(f"cannot start a build of {store}: {exc}") from exc
        try:
            try:
                result = _Build(repo, cfg, conn, target, fresh=full, download=download).run(started)
                graph_store.publish(conn, temp, store)
            except graph_git.ReadFailed as exc:
                raise BuildError(f"git could not read a blob of {target[:12]}: {exc}") from exc
            except (OSError, sqlite3.OperationalError) as exc:  # a directory at the store path, a full disk
                raise BuildError(f"cannot write the index at {store}: {exc}") from exc
        except BaseException:
            graph_store.discard(conn, temp)
            raise
    return result


class _Index:
    """`graph_resolve.Index` over the store: this build's facts first, then the cached facts of unchanged rows."""

    def __init__(self, conn, rows: Mapping[str, FileRow], fresh: Mapping[str, Facts | None]) -> None:
        self.conn, self.rows, self.fresh = conn, rows, fresh
        self._facts: dict[str, Facts | None] = {}
        self._providers: dict[str, list[str]] = {}

    def providers(self, key: str) -> Sequence[str]:
        if key not in self._providers:
            self._providers[key] = graph_store.providers(self.conn, key)
        return self._providers[key]

    def facts(self, path: str) -> Facts | None:
        if path in self.fresh:
            return self.fresh[path]
        if path not in self._facts:
            row = self.rows.get(path)
            indexed = row is not None and row.state == "indexed"
            self._facts[path] = graph_store.facts(self.conn, row.blob, row.lang, row.version) if indexed else None
        return self._facts[path]


def _judged(state_name: str) -> str:
    """A row's state as `graph_select.state` gives it: `unparsed` is an indexed path the parser did not read."""
    return "indexed" if state_name == "unparsed" else state_name


class _Build:
    """One build's working state over the temp copy of the store."""

    def __init__(self, repo: Path, cfg: Config, conn, commit: str, *, fresh: bool, download: bool) -> None:
        self.repo, self.conn, self.commit, self.fresh_store, self.download = repo, conn, commit, fresh, download
        self.repo_id = pack.repo_id(pack.checkout_root(repo))
        self.doc_roots = doc_roots(cfg.project.docs)
        self.limit = cfg.graph.max_file_kb * 1024
        self.exclude = Ignore({"": "\n".join(cfg.graph.exclude)})
        self.settings = json.dumps({"exclude": list(cfg.graph.exclude), "max_file_kb": cfg.graph.max_file_kb,
                                    "doc_roots": list(self.doc_roots), "repo_id": self.repo_id,
                                    "engine": pack.ENGINE_VERSION}, sort_keys=True)
        self.meta = graph_store.meta(conn)
        self.rows = graph_store.files(conn)  # as the last snapshot left them
        self.changed: dict[str, Entry | None] = {}  # None: deleted
        self.old_keys: dict[str, set[str]] = {}
        self.fresh: dict[str, Facts | None] = {}  # the paths given nodes this build, with their facts (None: unparsed)
        self.queue: list[tuple[str, Entry, str]] = []  # (path, entry, language) to extract
        self.notes: list[str] = []
        self.file_notes: list[str] = []
        self.parsed = self.cached = self.removed = 0
        self._listed: dict[str, Entry] | None = None

    def run(self, started: float) -> Result:
        previous = self.meta.get("commit") or None
        everything = self.fresh_store or previous is None or self.meta.get("settings") != self.settings
        self._change_list(previous, everything)
        ignore = self._ignore_files(everything)
        for path in sorted(self.changed):
            self._place(path, ignore)
        offered = self._extract()
        resolved = self._resolve()
        graph_store.set_meta(self.conn, commit=self.commit, built_at=f"{time.time():.0f}", engine=pack.ENGINE_VERSION,
                             languages=json.dumps(offered, sort_keys=True), settings=self.settings)
        graph_store.prune_facts(self.conn)
        final = graph_store.files(self.conn)
        left_out = Counter(row.state for row in final.values() if row.state not in _HAS_NODES)
        langs = {row.lang for row in final.values() if row.state in _HAS_NODES and row.lang}
        notes = [*self.notes, *sorted(self.file_notes), *ignore.notes, *self.exclude.notes]
        if len(notes) > NOTES_MAX:
            notes = [*notes[: NOTES_MAX - 1], f"{len(notes) - NOTES_MAX + 1} more note(s)"]
        return Result(
            commit=self.commit, previous=previous, full=everything, parsed=self.parsed, cached=self.cached,
            resolved=resolved, removed=self.removed, left_out=dict(sorted(left_out.items())),
            languages={lang: "exact" if lang in offered else "not parsed" for lang in sorted(langs)},
            notes=tuple(notes), ms=int((time.monotonic() - started) * 1000),
        )

    # --- 1. what changed ---

    def _listing(self) -> dict[str, Entry]:
        if self._listed is None:
            got = graph_git.listing(self.repo, self.commit)
            if got is None:
                raise BuildError(f"git cannot list the tree of {self.commit[:12]}")
            self._listed = {entry.path: entry for entry in got[0]}
            self.notes.extend(got[1])
        return self._listed

    def _mark(self, path: str, entry: Entry | None) -> None:
        """`path` joins the change list; the keys it provided are kept first, for the paths that wanted them."""
        if path in self.rows and path not in self.old_keys:
            self.old_keys[path] = graph_store.provided_by(self.conn, path)
        self.changed[path] = entry

    def _change_list(self, previous: str | None, everything: bool) -> None:
        diff = None
        if not everything and previous != self.commit:
            diff = graph_git.changes(self.repo, previous, self.commit)
        if diff is not None:
            for change in diff[0]:
                self._mark(change.path, None if change.new is None else Entry(change.path, change.mode, change.new))
            self.notes.extend(diff[1])
            return
        if not everything and previous == self.commit:
            return
        listed = self._listing()
        for path, entry in listed.items():
            row = self.rows.get(path)
            if everything or row is None or (row.blob, row.mode) != (entry.blob, entry.mode):
                self._mark(path, entry)
        for path in self.rows:
            if path not in listed:
                self._mark(path, None)

    # --- 2. the ignore files, and judging the paths again ---

    def _ignore_files(self, everything: bool) -> Ignore:
        """The ignore files of the commit, kept in the store; every path judged again when their texts moved."""
        stored = graph_store.ignore_files(self.conn)
        if not everything and not any(posixpath.basename(path) == ".gitignore" for path in self.changed):
            return Ignore(stored)
        listed = self._listing()
        found = sorted((path for path, entry in listed.items()
                        if posixpath.basename(path) == ".gitignore" and entry.mode in _REGULAR),
                       key=lambda path: (path.count("/"), path))
        texts: dict[str, str] = {}
        for _, level in itertools.groupby(found, key=lambda path: path.count("/")):
            above = Ignore(texts)  # the shallower files: git never loads an ignore file they ignore
            loaded = [path for path in level if not above.ignored(path)]
            unread = [path for path in loaded if path in self.changed or posixpath.dirname(path) not in stored]
            blobs = [listed[path].blob for path in unread]
            got = dict(graph_git.read_blobs(self.repo, blobs, IGNORE_MAX_BYTES)) if blobs else {}
            for path in loaded:
                directory = posixpath.dirname(path)
                if path not in unread:
                    texts[directory] = stored[directory]
                elif (data := got.get(listed[path].blob)) is None:
                    self.notes.append(f"{path}: over {IGNORE_MAX_BYTES} bytes, its patterns left out")
                else:
                    texts[directory] = data.decode("utf-8", "replace")
        moved = False
        for directory in sorted({*stored, *texts}):
            if stored.get(directory) != texts.get(directory):
                graph_store.set_ignore_file(self.conn, directory, texts.get(directory))
                moved = True
        ignore = Ignore(texts)
        if moved and not everything:
            for path, entry in listed.items():
                row = self.rows.get(path)
                if path not in self.changed and (row is None or _judged(row.state) != self._state(path, entry, ignore)):
                    self._mark(path, entry)
        return ignore

    def _state(self, path: str, entry: Entry, ignore: Ignore) -> str:
        return state(path, entry.mode, ignore=ignore, exclude=self.exclude)

    # --- 3. each changed path: dropped, left out, a file node, or queued for its facts ---

    def _place(self, path: str, ignore: Ignore) -> None:
        entry, row = self.changed[path], self.rows.get(path)
        had_nodes = row is not None and row.state in _HAS_NODES
        if entry is None:
            graph_store.drop_file(self.conn, path)
            self.removed += had_nodes
            return
        verdict = self._state(path, entry, ignore)
        if verdict != "indexed":
            graph_store.drop_file(self.conn, path)
            graph_store.put_file(self.conn, FileRow(path, entry.blob, entry.mode, "", verdict, "", ""))
            self.removed += had_nodes
            return
        lang = parses(path, self.doc_roots)
        if lang is None:
            self._unparsed(path, entry, "", "", "")
        else:
            self.queue.append((path, entry, lang))

    def _unparsed(self, path: str, entry: Entry, lang: str, version: str, note: str) -> None:
        graph_store.put_file(self.conn, FileRow(path, entry.blob, entry.mode, lang, "unparsed", version, note))
        self.fresh[path] = None
        if note:
            self.file_notes.append(f"{path}: {note}")

    def _indexed(self, path: str, entry: Entry, lang: str, version: str, facts: Facts) -> None:
        note = "; ".join(facts.notes)[:NOTE_MAX]
        graph_store.put_file(self.conn, FileRow(path, entry.blob, entry.mode, lang, "indexed", version, note))
        self.fresh[path] = facts

    def _known(self) -> dict[str, str]:
        """The versions the last build's parser offered, where this engine can trust them; {} otherwise."""
        if self.meta.get("engine") != pack.ENGINE_VERSION:
            return {}
        try:
            raw = json.loads(self.meta.get("languages", "{}"))
        except (ValueError, RecursionError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {lang: version for lang, version in raw.items()
                if lang in EXTRACTOR_VERSIONS and isinstance(version, str)
                and version.split("+")[0] == EXTRACTOR_VERSIONS[lang]}

    def _candidates(self) -> list[FileRow]:
        """The unchanged rows whose facts a parser gave or could give: indexed, or unparsed after a parser failure."""
        return [row for path, row in sorted(self.rows.items()) if path not in self.changed and row.lang
                and (row.state == "indexed" or (row.state == "unparsed" and row.note in _PARSE_FAILED))]

    def _extract(self) -> dict[str, str]:
        """Give every queued path its facts; the versions this build's parser offered (or, unstarted, the last one's)."""
        known = self._known()
        hits: dict[str, Facts] = {}
        for path, entry, lang in self.queue:
            version = known.get(lang)
            found = graph_store.facts(self.conn, entry.blob, lang, version) if version is not None else None
            if found is not None:
                hits[path] = found
        candidates = self._candidates()
        start = len(hits) < len(self.queue) or any(row.version != known.get(row.lang) for row in candidates)
        with (graph_parse.Parser(download=self.download) if start else contextlib.nullcontext()) as parser:
            offered = dict(parser.languages) if parser is not None else known
            for row in candidates:
                if row.lang in offered and row.version != offered[row.lang]:
                    entry = Entry(row.path, row.mode, row.blob)
                    self._mark(row.path, entry)
                    self.queue.append((row.path, entry, row.lang))
            reuse = {lang for lang, version in offered.items() if known.get(lang) == version}
            for (path, entry, lang), data in self._read(self.queue):
                if data is None:
                    self._unparsed(path, entry, lang, "", TOO_BIG)
                elif b"\0" in data[:SNIFF_BYTES]:
                    self._unparsed(path, entry, lang, "", BINARY)
                else:
                    self._facts(parser, path, entry, lang, data, offered, hits if lang in reuse else {})
        return offered

    def _read(self, queue: Sequence[tuple[str, Entry, str]]) -> Iterator[tuple[tuple[str, Entry, str], bytes | None]]:
        """Each queued item with its blob's bytes (None: over `max_file_kb`), from one `read_blobs`."""
        by_blob: dict[str, list[tuple[str, Entry, str]]] = {}
        for item in queue:
            by_blob.setdefault(item[1].blob, []).append(item)
        if not by_blob:
            return
        for blob, data in graph_git.read_blobs(self.repo, list(by_blob), self.limit):
            for item in by_blob.pop(blob, ()):
                yield item, data
        if by_blob:  # `read_blobs` answers for every id it is given; an id it skipped is a failed read, not data
            raise graph_git.ReadFailed(f"no answer for blob {next(iter(by_blob))}")

    def _facts(self, parser, path: str, entry: Entry, lang: str, data: bytes, offered: Mapping[str, str],
               hits: Mapping[str, Facts]) -> None:
        version = offered.get(lang)
        if version is None:
            self._unparsed(path, entry, lang, "", "no_grammar")
            return
        facts = hits.get(path) or graph_store.facts(self.conn, entry.blob, lang, version)
        if facts is not None:
            self.cached += 1
        else:
            facts = parser.parse(path, lang, data) if parser is not None else empty(lang, "parser_unavailable")
            self.parsed += parser is not None
            failed = facts.notes[0] if len(facts.notes) == 1 and facts.notes[0] in _PARSE_FAILED else None
            if failed is not None and facts == empty(lang, failed):
                self._unparsed(path, entry, lang, "" if failed in _NO_VERDICT else version, failed)
                return
            graph_store.put_facts(self.conn, entry.blob, lang, version, facts)
            self.file_notes.extend(f"{path}: {note}" for note in facts.notes)
        self._indexed(path, entry, lang, version, facts)

    # --- 4. keys and resolution ---

    def _resolve(self) -> int:
        """Set the keys of the changed paths, then resolve them and every path that wanted a key they touched."""
        given = sorted(path for path in self.changed if path in self.fresh)
        touched: set[str] = set().union(*self.old_keys.values())
        for path in given:
            facts = self.fresh[path]
            keys = provided(path, facts)
            touched |= keys
            graph_store.set_keys(self.conn, path, keys, wanted(path, facts) if facts is not None else ())
        affected = graph_store.wanting(self.conn, touched) - set(self.changed)
        index = _Index(self.conn, self.rows, self.fresh)
        count = 0
        for path in sorted({*given, *affected}):
            facts = index.facts(path)
            if facts is not None:
                nodes, edges = resolve(self.repo_id, path, facts, index)
            elif path in self.fresh:
                nodes, edges = [file_node(self.repo_id, path, None)], []
            else:  # an affected row whose cached facts are gone: its graph is left as it was
                self.file_notes.append(f"{path}: its cached facts are missing, not resolved again")
                continue
            graph_store.replace_graph(self.conn, path, nodes, edges)
            count += 1
        return count
