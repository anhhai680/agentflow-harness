"""The knowledge graph's store: one SQLite file per repository, built as a copy and swapped in by rename.

A build never writes the published file. `begin` copies it (or starts a new schema) into a temp file beside it,
the builder writes through the returned connection, and `publish` commits, closes, syncs and `os.replace`s the temp
file over the published one, so a reader sees the old snapshot or the new one and never a half-written one. A reader
(`open_read`) opens the file with `mode=ro`; one opened before a `publish` keeps reading the snapshot it opened.
The journal is SQLite's default rollback journal, so a snapshot is one self-contained file with no `-wal` or `-shm`.

The tables are `meta`, `files`, `facts` (a cache of what an extractor returned for one blob, kept past the blob's
last use so a revert is not parsed again), `provides` and `wants` (the keys a file offers and looks up), `nodes`,
`edges`, `ignore_files` and `search`, an FTS5 table whose rows share their rowid with `nodes`. An edge is stored
with the `origin` path whose facts made it, so dropping a file drops what it said, never what others said of it;
an edge whose other end is gone dangles until its origin is resolved again, and `neighbours` skips it.

Everything read from the repository ends up here as bound parameters, never as SQL text. A query is reduced to
`[a-z0-9]` tokens before it reaches FTS5. The directory is `0700` and the file `0600`, since it holds names,
signatures and doc text of the indexed repository.
"""

from __future__ import annotations

import functools
import json
import os
import re
import secrets
import sqlite3
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from agentflow import safeio
from agentflow.graph_model import SCHEMA_VERSION, Edge, Facts, Node, check_facts, to_json

FACTS_KEEP = 2000  # cached facts kept for blobs no longer in the tree (a revert is then not parsed again)
QUERY_TOKENS = 16
TOKEN_MAX = 64  # a query token is cut to this many characters
SEARCH_MAX = 200  # most hits one search returns
NEIGHBOURS_MAX = 200  # most rows one `neighbours` returns
DEPTH_MAX = 3
BM25 = "bm25(search, 0.0, 8.0, 6.0, 3.0, 1.0)"  # one weight per column: node_id, name, words, path, text
_CHUNK = 500  # bound parameters per IN list
_TABLES = ("meta", "files", "facts", "provides", "wants", "nodes", "edges", "ignore_files", "search")
_SCHEMA = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE files (path TEXT PRIMARY KEY, blob TEXT NOT NULL, mode TEXT NOT NULL, lang TEXT NOT NULL,"
    " state TEXT NOT NULL, version TEXT NOT NULL, note TEXT NOT NULL)",
    "CREATE TABLE facts (blob TEXT NOT NULL, lang TEXT NOT NULL, version TEXT NOT NULL, json TEXT NOT NULL,"
    " lines INTEGER NOT NULL, seen INTEGER NOT NULL, PRIMARY KEY (blob, lang, version))",
    "CREATE INDEX facts_seen ON facts (seen)",  # MAX(seen) on every put and hit, and prune's ORDER BY
    "CREATE TABLE provides (key TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY (key, path))",
    "CREATE INDEX provides_path ON provides (path)",
    "CREATE TABLE wants (key TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY (key, path))",
    "CREATE INDEX wants_path ON wants (path)",
    "CREATE TABLE nodes (id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, name TEXT NOT NULL, path TEXT NOT NULL,"
    " line INTEGER NOT NULL, end_line INTEGER NOT NULL, trust TEXT NOT NULL, signature TEXT NOT NULL,"
    " text TEXT NOT NULL, blob TEXT NOT NULL)",
    "CREATE INDEX nodes_path ON nodes (path)",
    "CREATE TABLE edges (src TEXT NOT NULL, dst TEXT NOT NULL, kind TEXT NOT NULL, confidence TEXT NOT NULL,"
    " origin TEXT NOT NULL, PRIMARY KEY (src, dst, kind, origin))",
    "CREATE INDEX edges_dst ON edges (dst)",
    "CREATE INDEX edges_origin ON edges (origin)",
    "CREATE TABLE ignore_files (directory TEXT PRIMARY KEY, text TEXT NOT NULL)",
    "CREATE VIRTUAL TABLE search USING fts5(node_id UNINDEXED, name, words, path, text, tokenize='unicode61')",
)


class Unusable(Exception):
    """The store is missing, not SQLite, of another schema version, or this SQLite has no FTS5."""


@functools.cache
def fts5_available() -> bool:
    """Whether this SQLite can create an FTS5 table."""
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE probe USING fts5(a)")
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return True


def path_for(state_dir: Path) -> Path:
    """`<state dir>/graph/graph.sqlite`."""
    return Path(state_dir) / "graph" / "graph.sqlite"


@dataclass(frozen=True)
class FileRow:
    path: str
    blob: str
    mode: str
    lang: str
    state: str
    version: str
    note: str


@dataclass(frozen=True)
class Hit:
    node: Node
    score: float  # bm25: the lower, the better


# --- opening, building, publishing ---


def _harden(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA trusted_schema = OFF")  # a planted file's views and triggers may not call functions


def open_read(path: Path) -> sqlite3.Connection:
    """`path` opened read-only; `Unusable` when it is not a regular file, not a store of this schema, or has no FTS5."""
    path = Path(path)
    if not fts5_available():
        raise Unusable("this SQLite has no FTS5")
    try:
        info = os.lstat(path)
        absolute = os.path.abspath(path)
    except (OSError, ValueError) as exc:
        raise Unusable(f"no graph at {path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise Unusable(f"{path} is not a regular file")
    # The path is percent-encoded so a `?` or `#` in a directory name cannot add URI parameters.
    uri = f"file:{quote(absolute, safe='/')}?mode=ro"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(uri, uri=True)
        _harden(conn)
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not set(_TABLES) <= names:
            raise Unusable(f"{path} is not a graph store")
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
        if row is None or row[0] != str(SCHEMA_VERSION):
            raise Unusable(f"{path} has another schema version")
        conn.execute("SELECT 1 FROM search LIMIT 1").fetchall()
    except (sqlite3.Error, ValueError) as exc:
        if conn is not None:
            conn.close()
        raise Unusable(f"{path} cannot be read as a graph store: {exc}") from exc
    except Unusable:
        if conn is not None:
            conn.close()
        raise
    return conn


def _private_dir(directory: Path) -> None:
    """Make `directory` exist as a real directory of mode 0700 (its parents are made as ordinary directories)."""
    directory = Path(directory)
    if directory.is_symlink():
        raise safeio.SymlinkRefused(f"refusing to use a symlink at {directory}")
    safeio.ensure_dir(directory.parent)
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    if directory.is_symlink() or not directory.is_dir():
        raise safeio.SymlinkRefused(f"{directory} is not a directory")
    os.chmod(directory, 0o700)


def _temp_prefix(path: Path) -> str:
    return f"{path.name}.tmp-"


def _remove_stale(path: Path) -> None:
    """Remove what killed builds left beside `path`: its temp files and their journals, nothing else."""
    prefix = _temp_prefix(path)
    try:
        names = os.listdir(path.parent)
    except OSError:
        return
    for name in names:
        if not name.startswith(prefix):
            continue
        stale = path.parent / name
        try:
            if not stat.S_ISDIR(os.lstat(stale).st_mode):
                stale.unlink()
        except OSError:
            pass


def _create_temp(path: Path) -> Path:
    """A new empty temp file beside `path`, created exclusively with mode 0600 (never wider, whatever the umask)."""
    for _ in range(8):
        temp = path.parent / f"{_temp_prefix(path)}{os.getpid()}-{secrets.token_hex(6)}"
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return temp
    raise OSError(f"cannot create a temp file beside {path}")


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def begin(path: Path, fresh: bool = False) -> tuple[sqlite3.Connection, Path]:
    """A writable copy of the store at `path`, as a temp file beside it, with a transaction open.

    A new schema when `fresh` or when `path` is `Unusable`. Temp files of killed builds are removed first: one builder
    runs at a time. Hand the result to `publish` or `discard`. `Unusable` when this SQLite has no FTS5.
    """
    path = Path(path)
    if not fts5_available():
        raise Unusable("this SQLite has no FTS5")
    _private_dir(path.parent)
    _remove_stale(path)
    source: sqlite3.Connection | None = None
    if not fresh:
        try:
            source = open_read(path)
        except Unusable:
            source = None
    conn: sqlite3.Connection | None = None
    temp: Path | None = None
    try:
        temp = _create_temp(path)
        conn = sqlite3.connect(str(temp), isolation_level=None)
        _harden(conn)
        conn.execute("PRAGMA journal_mode = DELETE")
        if source is not None:
            source.backup(conn)
        else:
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute("INSERT INTO meta (key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        conn.execute("PRAGMA synchronous = OFF")  # `publish` syncs the file once, at the end
        conn.execute("BEGIN")
    except BaseException:
        if conn is not None:
            conn.close()
        if temp is not None:
            _unlink(temp)
            _unlink(temp.with_name(temp.name + "-journal"))
        raise
    finally:
        if source is not None:
            source.close()
    return conn, temp


def _fsync(path: Path, flags: int = os.O_RDONLY) -> None:
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(conn: sqlite3.Connection, temp: Path, path: Path) -> None:
    """Commit and close `conn`, sync `temp`, rename it over `path` and sync the directory.

    On any failure `temp` is removed and `path` is untouched.
    """
    temp, path = Path(temp), Path(path)
    try:
        if conn.in_transaction:
            conn.execute("COMMIT")
        conn.close()
        _fsync(temp)
        os.replace(temp, path)
    except BaseException:
        discard(conn, temp)
        raise
    try:
        _fsync(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        pass  # the rename is done; a filesystem that cannot sync a directory is not a failed build


def discard(conn: sqlite3.Connection, temp: Path) -> None:
    """Close `conn` and remove `temp` (and its journal); the published file is not touched."""
    temp = Path(temp)
    try:
        conn.close()
    except sqlite3.Error:
        pass
    _unlink(temp)
    _unlink(temp.with_name(temp.name + "-journal"))


# --- meta, files, ignore files ---


def meta(conn: sqlite3.Connection) -> dict[str, str]:
    return {key: value for key, value in conn.execute("SELECT key, value FROM meta")}


def set_meta(conn: sqlite3.Connection, **values: str) -> None:
    conn.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", [(k, str(v)) for k, v in values.items()])


def files(conn: sqlite3.Connection) -> dict[str, FileRow]:
    rows = conn.execute("SELECT path, blob, mode, lang, state, version, note FROM files")
    return {row[0]: FileRow(*row) for row in rows}


def put_file(conn: sqlite3.Connection, row: FileRow) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO files (path, blob, mode, lang, state, version, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (row.path, row.blob, row.mode, row.lang, row.state, row.version, row.note),
    )
    conn.execute("UPDATE nodes SET blob = ? WHERE path = ?", (row.blob, row.path))


def ignore_files(conn: sqlite3.Connection) -> dict[str, str]:
    return {directory: text for directory, text in conn.execute("SELECT directory, text FROM ignore_files")}


def set_ignore_file(conn: sqlite3.Connection, directory: str, text: str | None) -> None:
    if text is None:
        conn.execute("DELETE FROM ignore_files WHERE directory = ?", (directory,))
    else:
        conn.execute("INSERT OR REPLACE INTO ignore_files (directory, text) VALUES (?, ?)", (directory, text))


# --- the facts cache ---


def _max_line(facts: Facts) -> int:
    lines = [1]
    for d in facts.defs:
        lines += [d.line, d.end]
    for s in facts.sections:
        lines += [s.line, s.end]
    lines += [i.line for i in facts.imports]
    lines += [r.line for r in facts.refs]
    lines += [link.line for link in facts.links]
    return max(lines)


def _next_seen() -> str:
    return "COALESCE((SELECT MAX(seen) FROM facts), 0) + 1"


def facts(conn: sqlite3.Connection, blob: str, lang: str, version: str) -> Facts | None:
    """The cached facts of `blob`, or None on a miss or when the stored row no longer passes `check_facts`."""
    row = conn.execute("SELECT json, lines FROM facts WHERE blob = ? AND lang = ? AND version = ?", (blob, lang, version)).fetchone()
    if row is None:
        return None
    try:
        found = check_facts(json.loads(row[0]), lang, row[1])
    except (ValueError, RecursionError):
        return None
    if found is not None:
        try:  # a read-only connection cannot note that the row was used; that is no reason to fail
            conn.execute(
                f"UPDATE facts SET seen = {_next_seen()} WHERE blob = ? AND lang = ? AND version = ?", (blob, lang, version)
            )
        except sqlite3.OperationalError:
            pass
    return found


def put_facts(conn: sqlite3.Connection, blob: str, lang: str, version: str, facts: Facts) -> None:
    """Cache `facts` for `blob`. The row keeps the highest line the facts name, as the line count `check_facts` needs."""
    payload = json.dumps(to_json(facts), sort_keys=True)
    conn.execute(
        f"INSERT OR REPLACE INTO facts (blob, lang, version, json, lines, seen) VALUES (?, ?, ?, ?, ?, {_next_seen()})",
        (blob, lang, version, payload, _max_line(facts)),
    )


def prune_facts(conn: sqlite3.Connection) -> int:
    """Drop cached facts past `FACTS_KEEP` for blobs no `files` row names (least recently seen first); the count dropped."""
    orphan = "blob NOT IN (SELECT blob FROM files)"
    cursor = conn.execute(
        f"DELETE FROM facts WHERE {orphan} AND rowid NOT IN "
        f"(SELECT rowid FROM facts WHERE {orphan} ORDER BY seen DESC, blob, lang, version LIMIT ?)",
        (FACTS_KEEP,),
    )
    return max(cursor.rowcount, 0)


# --- keys ---


def set_keys(conn: sqlite3.Connection, path: str, provided: Iterable[str], wanted: Iterable[str]) -> None:
    conn.execute("DELETE FROM provides WHERE path = ?", (path,))
    conn.execute("DELETE FROM wants WHERE path = ?", (path,))
    conn.executemany("INSERT OR IGNORE INTO provides (key, path) VALUES (?, ?)", [(key, path) for key in provided])
    conn.executemany("INSERT OR IGNORE INTO wants (key, path) VALUES (?, ?)", [(key, path) for key in wanted])


def provided_by(conn: sqlite3.Connection, path: str) -> set[str]:
    return {row[0] for row in conn.execute("SELECT key FROM provides WHERE path = ?", (path,))}


def wanting(conn: sqlite3.Connection, keys: Iterable[str]) -> set[str]:
    """The paths that want any of `keys`."""
    keys = sorted(set(keys))
    found: set[str] = set()
    for start in range(0, len(keys), _CHUNK):
        chunk = keys[start : start + _CHUNK]
        marks = ",".join("?" * len(chunk))
        found.update(row[0] for row in conn.execute(f"SELECT path FROM wants WHERE key IN ({marks})", chunk))
    return found


def providers(conn: sqlite3.Connection, key: str) -> list[str]:
    return [row[0] for row in conn.execute("SELECT path FROM provides WHERE key = ? ORDER BY path", (key,))]


# --- the graph ---


def _drop_nodes(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM search WHERE rowid IN (SELECT rowid FROM nodes WHERE path = ?)", (path,))
    conn.execute("DELETE FROM nodes WHERE path = ?", (path,))


def drop_file(conn: sqlite3.Connection, path: str) -> None:
    """Forget `path`: its row, its nodes and search rows, the edges it originated and its keys. Cached facts stay."""
    _drop_nodes(conn, path)
    conn.execute("DELETE FROM edges WHERE origin = ?", (path,))
    conn.execute("DELETE FROM provides WHERE path = ?", (path,))
    conn.execute("DELETE FROM wants WHERE path = ?", (path,))
    conn.execute("DELETE FROM files WHERE path = ?", (path,))


_WORDS = (re.compile(r"([a-z0-9])([A-Z])"), re.compile(r"([A-Z]+)([A-Z][a-z])"))


def _tokens(text: str) -> list[str]:
    """Lower-case `[a-z0-9]` words of `text`, split at camel-case boundaries first (`RunCapHold` is run, cap, hold)."""
    for pattern in _WORDS:
        text = pattern.sub(r"\1 \2", text)
    return re.findall(r"[a-z0-9]+", text.lower())


def replace_graph(conn: sqlite3.Connection, path: str, nodes: Sequence[Node], edges: Sequence[Edge]) -> None:
    """Make `nodes` the nodes of `path` and `edges` the edges `path` originated, replacing what was there.

    A node whose `path` or an edge whose `origin` is another path is a caller bug (`ValueError`, nothing changed).
    A repeated node id keeps the last node; a repeated edge keeps the last one.
    """
    if any(n.path != path for n in nodes) or any(e.origin != path for e in edges):
        raise ValueError(f"replace_graph({path!r}) was given a node or an edge of another path")
    _drop_nodes(conn, path)
    conn.execute("DELETE FROM edges WHERE origin = ?", (path,))
    row = conn.execute("SELECT blob FROM files WHERE path = ?", (path,)).fetchone()
    blob = row[0] if row else ""
    for n in sorted({n.id: n for n in nodes}.values(), key=lambda n: n.id):
        old = conn.execute("SELECT rowid FROM nodes WHERE id = ?", (n.id,)).fetchone()
        if old is not None:  # the id is another path's: it moves here, and its search row goes with the old node
            conn.execute("DELETE FROM search WHERE rowid = ?", (old[0],))
            conn.execute("DELETE FROM nodes WHERE rowid = ?", (old[0],))
        cursor = conn.execute(
            "INSERT INTO nodes (id, kind, name, path, line, end_line, trust, signature, text, blob) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (n.id, n.kind, n.name, n.path, n.line, n.end, n.trust, n.signature, n.text, blob),
        )
        conn.execute(
            "INSERT INTO search (rowid, node_id, name, words, path, text) VALUES (?, ?, ?, ?, ?, ?)",
            (cursor.lastrowid, n.id, n.name, " ".join(_tokens(n.name)), n.path, n.text),
        )
    conn.executemany(
        "INSERT OR REPLACE INTO edges (src, dst, kind, confidence, origin) VALUES (?, ?, ?, ?, ?)",
        [(e.src, e.dst, e.kind, e.confidence, e.origin) for e in edges],
    )


# --- reading the graph ---

_NODE = "id, kind, name, path, line, end_line, trust, signature, text"


def _node(row: Sequence) -> Node:
    return Node(*row)


def node(conn: sqlite3.Connection, node_id: str) -> Node | None:
    row = conn.execute(f"SELECT {_NODE} FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return _node(row) if row else None


def nodes_at(conn: sqlite3.Connection, path: str) -> list[Node]:
    return [_node(row) for row in conn.execute(f"SELECT {_NODE} FROM nodes WHERE path = ? ORDER BY line, id", (path,))]


def search(conn: sqlite3.Connection, query: str, kind: str | None = None, limit: int = 10) -> list[Hit]:
    """Nodes matching any word of `query`, best first (bm25, then node id). `kind` keeps one node kind.

    The query becomes at most `QUERY_TOKENS` distinct `[a-z0-9]` words (camel case split), each double-quoted and
    joined with OR, as one bound `MATCH` parameter; a query with no word finds nothing. It never raises on odd text.
    """
    tokens = list(dict.fromkeys(token[:TOKEN_MAX] for token in _tokens(query)))[:QUERY_TOKENS]
    limit = min(int(limit), SEARCH_MAX)
    if not tokens or limit <= 0:
        return []
    match = " OR ".join(f'"{token}"' for token in tokens)
    sql = (
        f"SELECT n.id, n.kind, n.name, n.path, n.line, n.end_line, n.trust, n.signature, n.text, {BM25} AS score "
        "FROM search JOIN nodes n ON n.rowid = search.rowid WHERE search MATCH ?"
    )
    params: list[object] = [match]
    if kind is not None:
        sql += " AND n.kind = ?"
        params.append(kind)
    sql += " ORDER BY score, n.id LIMIT ?"
    params.append(limit)
    return [Hit(_node(row[:9]), float(row[9])) for row in conn.execute(sql, params)]


def neighbours(conn: sqlite3.Connection, node_id: str, depth: int = 1) -> list[tuple[Edge, Node]]:
    """The edges within `depth` (1 to 3) hops of `node_id`, either direction, each with the node at its far end.

    Breadth first, level by level, each level in edge order, so the result is deterministic; at most
    `NEIGHBOURS_MAX` rows. An edge whose far end is not a node is skipped.
    """
    depth = max(1, min(int(depth), DEPTH_MAX))
    if node(conn, node_id) is None:
        return []
    seen_nodes = {node_id}
    seen_edges: set[tuple[str, str, str, str]] = set()
    frontier = [node_id]
    out: list[tuple[Edge, Node]] = []
    for _ in range(depth):
        found: list[tuple[tuple[str, str, str, str, str], str]] = []
        for here in frontier:
            for column, other in (("src", "dst"), ("dst", "src")):
                for row in conn.execute(
                    f"SELECT src, dst, kind, confidence, origin FROM edges WHERE {column} = ? ORDER BY src, dst, kind, origin LIMIT ?",
                    (here, NEIGHBOURS_MAX),
                ):
                    found.append((row, row[1] if other == "dst" else row[0]))
        next_frontier: list[str] = []
        for row, far in sorted(found):
            key = (row[0], row[1], row[2], row[4])
            if key in seen_edges:
                continue
            target = node(conn, far)
            if target is None:
                continue
            seen_edges.add(key)
            out.append((Edge(*row), target))
            if len(out) >= NEIGHBOURS_MAX:
                return out
            if far not in seen_nodes:
                seen_nodes.add(far)
                next_frontier.append(far)
        frontier = sorted(next_frontier)
        if not frontier:
            break
    return out


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """`nodes`, `edges`, `files` (all) and `files:<state>` for each state in use."""
    out = {
        "nodes": conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
        "edges": conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
        "files": conn.execute("SELECT COUNT(*) FROM files").fetchone()[0],
    }
    for state, number in conn.execute("SELECT state, COUNT(*) FROM files GROUP BY state"):
        out[f"files:{state}"] = number
    return out


_DUMPED = {
    "meta": "key, value",
    "files": "path, blob, mode, lang, state, version, note",
    "facts": "blob, lang, version, json, lines",
    "provides": "key, path",
    "wants": "key, path",
    "nodes": "id, kind, name, path, line, end_line, trust, signature, text, blob",
    "edges": "src, dst, kind, confidence, origin",
    "ignore_files": "directory, text",
    "search": "node_id, name, words, path, text",
}


def dump(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """Every table's rows, sorted, with no rowid, counter or insertion order in them: stores built by different
    routes (a full build, a delta) hold the same graph exactly when their dumps are equal. `facts` is a cache that
    depends on history, and `meta` holds `built_at`; a comparison that wants only the graph skips those two."""
    return {table: sorted(tuple(row) for row in conn.execute(f"SELECT {columns} FROM {table}")) for table, columns in _DUMPED.items()}
