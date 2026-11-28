"""graph_store: the snapshot, the swap by rename, and the FTS5 search."""

from __future__ import annotations

import os
import sqlite3
import stat

import pytest

from agentflow import graph_store as gs
from agentflow.graph_model import Def, Edge, Facts, Import, Link, Node, Section, SCHEMA_VERSION


def node(path: str, name: str = "", kind: str = "file", text: str = "", line: int = 1, repo: str = "r") -> Node:
    return Node(f"{repo}:{kind}:{path}#{name}", kind, name or path, path, line, line, "code", "", text)


def edge(src: Node, dst: Node, origin: str, kind: str = "imports") -> Edge:
    return Edge(src.id, dst.id, kind, "extracted", origin)


def row(path: str, blob: str = "b", state: str = "parsed") -> gs.FileRow:
    return gs.FileRow(path, blob, "100644", "python", state, "1", "")


@pytest.fixture
def store(tmp_path):
    return gs.path_for(tmp_path / "state")


def build(path, fill=None, fresh=False):
    conn, temp = gs.begin(path, fresh=fresh)
    if fill:
        fill(conn)
    gs.publish(conn, temp, path)


@pytest.fixture
def conn(store):
    c, temp = gs.begin(store)
    yield c
    gs.discard(c, temp)


# --- snapshots ---


def test_begin_on_missing_path_gives_an_empty_schema_and_publish_makes_it_readable(store):
    conn, temp = gs.begin(store)
    assert gs.counts(conn) == {"nodes": 0, "edges": 0, "files": 0}
    assert gs.meta(conn) == {"schema": str(SCHEMA_VERSION)}
    gs.publish(conn, temp, store)
    reader = gs.open_read(store)
    try:
        assert gs.meta(reader)["schema"] == str(SCHEMA_VERSION)
    finally:
        reader.close()
    assert not temp.exists()


def test_published_file_is_0600_and_its_directory_0700(store):
    build(store)
    assert stat.S_IMODE(os.stat(store).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.parent).st_mode) == 0o700


def test_temp_file_is_private_from_the_start(store):
    old = os.umask(0)
    try:
        conn, temp = gs.begin(store)
        try:
            assert stat.S_IMODE(os.stat(temp).st_mode) == 0o600
        finally:
            gs.discard(conn, temp)
    finally:
        os.umask(old)


def test_publish_leaves_no_sidecar_files(store):
    build(store, lambda c: gs.put_file(c, row("a.py")))
    assert sorted(p.name for p in store.parent.iterdir()) == ["graph.sqlite"]


def test_reader_opened_before_a_swap_keeps_its_snapshot(store):
    build(store, lambda c: gs.put_file(c, row("a.py")))
    before = gs.open_read(store)
    build(store, lambda c: gs.put_file(c, row("b.py")))
    after = gs.open_read(store)
    try:
        assert sorted(gs.files(before)) == ["a.py"]
        assert sorted(gs.files(after)) == ["a.py", "b.py"]
    finally:
        before.close()
        after.close()


def test_discard_leaves_the_published_file_byte_identical(store):
    build(store, lambda c: gs.put_file(c, row("a.py")))
    data = store.read_bytes()
    conn, temp = gs.begin(store)
    gs.put_file(conn, row("z.py"))
    gs.discard(conn, temp)
    assert store.read_bytes() == data
    assert not temp.exists()
    assert sorted(p.name for p in store.parent.iterdir()) == ["graph.sqlite"]


def test_begin_removes_stale_temp_files_and_nothing_else(store):
    build(store)
    stale = store.parent / "graph.sqlite.tmp-123-abc"
    journal = store.parent / "graph.sqlite.tmp-123-abc-journal"
    other = store.parent / "notes.txt"
    near = store.parent / "graph.sqlite.bak"
    for f in (stale, journal, other, near):
        f.write_bytes(b"x")
    conn, temp = gs.begin(store)
    try:
        assert not stale.exists() and not journal.exists()
        assert other.exists() and near.exists() and store.exists()
    finally:
        gs.discard(conn, temp)


def test_begin_copies_the_published_snapshot_unless_fresh_or_unusable(store):
    build(store, lambda c: gs.put_file(c, row("a.py")))
    conn, temp = gs.begin(store)
    assert list(gs.files(conn)) == ["a.py"]
    gs.discard(conn, temp)
    conn, temp = gs.begin(store, fresh=True)
    assert gs.files(conn) == {}
    gs.discard(conn, temp)
    store.write_bytes(os.urandom(100))
    conn, temp = gs.begin(store)
    assert gs.files(conn) == {}
    gs.publish(conn, temp, store)
    gs.open_read(store).close()


def test_begin_publish_work_in_a_directory_named_like_a_uri(tmp_path):
    root = tmp_path / "root"
    state = root / "s?mode=rwc#frag%20x"
    path = gs.path_for(state)
    build(path, lambda c: gs.put_file(c, row("a.py")))
    reader = gs.open_read(path)
    try:
        assert list(gs.files(reader)) == ["a.py"]
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO meta (key, value) VALUES ('x', 'y')")
    finally:
        reader.close()
    assert sorted(p.name for p in root.iterdir()) == [state.name]


def test_begin_refuses_a_symlinked_graph_directory(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "graph").symlink_to(real)
    with pytest.raises(OSError):
        gs.begin(tmp_path / "graph" / "graph.sqlite")


def _zero_middle_pages(path) -> None:
    """Zero the pages of the `nodes` table: the header and the schema stay intact, so `open_read` accepts it."""
    conn = sqlite3.connect(path)
    root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'nodes'").fetchone()[0]
    size = conn.execute("PRAGMA page_size").fetchone()[0]
    conn.close()
    data = bytearray(path.read_bytes())
    assert root > 2 and len(data) >= root * size
    data[(root - 1) * size : root * size] = bytes(size)
    path.write_bytes(bytes(data))


def test_begin_starts_fresh_over_a_store_that_fails_its_quick_check(store):
    def fill(c):
        gs.set_meta(c, commit="c" * 40)
        for i in range(50):
            gs.replace_graph(c, f"p{i}.py", [node(f"p{i}.py", text="t" * 200)], [])

    build(store, fill)
    _zero_middle_pages(store)
    gs.open_read(store).close()  # it still opens: only a check of every page finds the damage
    conn, temp = gs.begin(store)
    try:
        assert gs.meta(conn) == {"schema": str(SCHEMA_VERSION)} and gs.counts(conn)["nodes"] == 0
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        gs.discard(conn, temp)


def test_a_store_inside_the_checkout_is_unusable(tmp_path):
    checkout = tmp_path / "repo"
    store = gs.path_for(checkout / "state")
    build(store, lambda c: gs.set_meta(c, commit="c" * 40))
    gs.open_read(store).close()  # without a checkout to compare with, it is an ordinary store
    for inside in (checkout, tmp_path / "link"):
        if inside.name == "link":
            inside.symlink_to(checkout)  # the checkout named through a symlink is the same checkout
        with pytest.raises(gs.Unusable, match=r"paths\.state_dir"):
            gs.open_read(store, checkout=inside)
        with pytest.raises(gs.Unusable, match=r"paths\.state_dir"):
            gs.begin(store, checkout=inside)
    assert sorted(p.name for p in store.parent.iterdir()) == ["graph.sqlite"]  # begin left no temp file
    gs.open_read(store, checkout=tmp_path / "elsewhere").close()
    conn, temp = gs.begin(store, checkout=tmp_path / "elsewhere")
    gs.discard(conn, temp)


@pytest.mark.skipif(not hasattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"), reason="this sqlite3 has no setconfig")
def test_every_connection_the_store_opens_is_defensive(store):
    conn, temp = gs.begin(store)
    assert conn.getconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE)
    gs.publish(conn, temp, store)
    reader = gs.open_read(store)
    try:
        assert reader.getconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE)
    finally:
        reader.close()


# --- open_read refuses what is not a usable store ---


def test_open_read_refuses_a_missing_file(store):
    with pytest.raises(gs.Unusable):
        gs.open_read(store)


def test_open_read_refuses_a_directory(tmp_path):
    with pytest.raises(gs.Unusable):
        gs.open_read(tmp_path)


def test_open_read_refuses_random_bytes(tmp_path):
    bad = tmp_path / "g.sqlite"
    bad.write_bytes(os.urandom(100))
    with pytest.raises(gs.Unusable):
        gs.open_read(bad)


def test_open_read_refuses_a_symlink(store, tmp_path):
    build(store)
    link = tmp_path / "link.sqlite"
    link.symlink_to(store)
    with pytest.raises(gs.Unusable):
        gs.open_read(link)


def test_open_read_refuses_another_schema_version(store):
    build(store, lambda c: gs.set_meta(c, schema="999"))
    with pytest.raises(gs.Unusable):
        gs.open_read(store)


def test_open_read_refuses_a_database_without_the_tables(tmp_path):
    odd = tmp_path / "odd.sqlite"
    c = sqlite3.connect(odd)
    c.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    c.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
    c.commit()
    c.close()
    with pytest.raises(gs.Unusable):
        gs.open_read(odd)


def test_without_fts5_the_store_is_unusable(store, monkeypatch):
    build(store)
    monkeypatch.setattr(gs, "fts5_available", lambda: False)
    with pytest.raises(gs.Unusable):
        gs.open_read(store)
    with pytest.raises(gs.Unusable):
        gs.begin(store)


def test_a_connection_from_open_read_refuses_writes(store):
    build(store)
    reader = gs.open_read(store)
    try:
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO meta (key, value) VALUES ('x', 'y')")
    finally:
        reader.close()


# --- the graph ---


def test_replace_then_drop_leaves_nothing_of_that_path(conn):
    a, b = node("a.py"), node("b.py")
    a_sym = node("a.py", "hold", "symbol", text="holds")
    gs.put_file(conn, row("a.py"))
    gs.put_file(conn, row("b.py"))
    gs.replace_graph(conn, "a.py", [a, a_sym], [edge(a, b, "a.py"), edge(a, a_sym, "a.py", "contains")])
    gs.replace_graph(conn, "b.py", [b], [edge(b, a, "b.py")])
    gs.set_keys(conn, "a.py", ["py:a"], ["py:b"])
    gs.set_keys(conn, "b.py", ["py:b"], [])
    gs.drop_file(conn, "a.py")
    assert gs.nodes_at(conn, "a.py") == []
    assert [n.id for n in gs.nodes_at(conn, "b.py")] == [b.id]
    assert conn.execute("SELECT COUNT(*) FROM edges WHERE origin = 'a.py'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM edges WHERE origin = 'b.py'").fetchone()[0] == 1
    assert gs.provided_by(conn, "a.py") == set() and gs.provided_by(conn, "b.py") == {"py:b"}
    assert gs.wanting(conn, ["py:b"]) == set()
    assert [h.node.id for h in gs.search(conn, "hold")] == []
    assert list(gs.files(conn)) == ["b.py"]
    assert gs.dump(conn)["search"] == [(b.id, "b.py", "b py", "b.py", "")]


def test_dropping_a_target_keeps_the_edges_others_made_to_it(conn):
    a, b = node("a.py"), node("b.py")
    gs.replace_graph(conn, "a.py", [a], [edge(a, b, "a.py")])
    gs.replace_graph(conn, "b.py", [b], [])
    gs.drop_file(conn, "b.py")
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 1
    assert gs.neighbours(conn, a.id) == []  # the far end is gone: the edge dangles and is skipped


def test_replace_graph_twice_leaves_one_copy(conn):
    a, sym = node("a.py"), node("a.py", "hold", "symbol", text="holds")
    for _ in range(2):
        gs.replace_graph(conn, "a.py", [a, sym], [edge(a, sym, "a.py", "contains")])
    assert gs.counts(conn)["nodes"] == 2 and gs.counts(conn)["edges"] == 1
    assert len(gs.dump(conn)["search"]) == 2
    assert len(gs.search(conn, "hold")) == 1


def test_replace_graph_survives_duplicate_node_ids_and_edges(conn):
    a = node("a.py")
    again = Node(a.id, a.kind, "renamed", a.path, 1, 1, "code", "", "")
    e = edge(a, a, "a.py", "references")
    gs.replace_graph(conn, "a.py", [a, again], [e, e])
    assert [n.name for n in gs.nodes_at(conn, "a.py")] == ["renamed"]
    assert gs.counts(conn)["edges"] == 1
    assert len(gs.dump(conn)["search"]) == 1


def test_a_node_id_moving_to_another_path_leaves_no_orphan_search_row(conn):
    first = node("a.py")
    moved = Node(first.id, "file", "a.py", "b.py", 1, 1, "code", "", "")
    gs.replace_graph(conn, "a.py", [first], [])
    gs.replace_graph(conn, "b.py", [moved], [])
    assert len(gs.dump(conn)["search"]) == 1
    assert gs.nodes_at(conn, "a.py") == [] and len(gs.nodes_at(conn, "b.py")) == 1


def test_replace_graph_refuses_a_node_or_edge_of_another_path(conn):
    a, b = node("a.py"), node("b.py")
    gs.replace_graph(conn, "a.py", [a], [])
    with pytest.raises(ValueError):
        gs.replace_graph(conn, "a.py", [b], [])
    with pytest.raises(ValueError):
        gs.replace_graph(conn, "a.py", [a], [edge(a, b, "b.py")])
    assert [n.id for n in gs.nodes_at(conn, "a.py")] == [a.id]


def test_nodes_carry_their_files_blob_whichever_is_written_first(conn):
    a = node("a.py")
    gs.replace_graph(conn, "a.py", [a], [])
    gs.put_file(conn, row("a.py", blob="late"))
    assert conn.execute("SELECT blob FROM nodes").fetchone()[0] == "late"
    gs.replace_graph(conn, "a.py", [a], [])
    assert conn.execute("SELECT blob FROM nodes").fetchone()[0] == "late"


def test_keys_over_three_files(conn):
    gs.set_keys(conn, "a.py", ["py:x", "path:a.py"], ["py:y"])
    gs.set_keys(conn, "b.py", ["py:x", "path:b.py"], ["py:y", "py:z"])
    gs.set_keys(conn, "c.py", ["py:y"], ["py:x"])
    assert gs.providers(conn, "py:x") == ["a.py", "b.py"]
    assert gs.providers(conn, "nothing") == []
    assert gs.provided_by(conn, "b.py") == {"py:x", "path:b.py"}
    assert gs.wanting(conn, ["py:y"]) == {"a.py", "b.py"}
    assert gs.wanting(conn, ["py:z", "py:x"]) == {"b.py", "c.py"}
    assert gs.wanting(conn, []) == set()
    gs.set_keys(conn, "a.py", ["py:q"], [])
    assert gs.providers(conn, "py:x") == ["b.py"] and gs.provided_by(conn, "a.py") == {"py:q"}
    assert gs.wanting(conn, ["py:y"]) == {"b.py"}


def test_wanting_handles_more_keys_than_one_chunk(conn):
    keys = [f"k{i}" for i in range(gs._CHUNK * 2 + 7)]
    gs.set_keys(conn, "a.py", [], keys[-3:])
    assert gs.wanting(conn, keys) == {"a.py"}


def test_ignore_files_set_replace_and_clear(conn):
    gs.set_ignore_file(conn, "", "*.log")
    gs.set_ignore_file(conn, "sub", "x")
    gs.set_ignore_file(conn, "sub", "y")
    assert gs.ignore_files(conn) == {"": "*.log", "sub": "y"}
    gs.set_ignore_file(conn, "sub", None)
    assert gs.ignore_files(conn) == {"": "*.log"}


def test_counts_by_state(conn):
    gs.put_file(conn, row("a.py"))
    gs.put_file(conn, row("b.py"))
    gs.put_file(conn, row("c.bin", state="skipped"))
    assert gs.counts(conn) == {"nodes": 0, "edges": 0, "files": 3, "files:parsed": 2, "files:skipped": 1}


# --- the facts cache ---

FACTS = Facts(
    lang="python",
    doc="Run cap.",
    defs=(Def("hold", "function", 3, 5, "def hold()", "holds", True),),
    imports=(Import("pkg.slots", "lease", "", 0, 1),),
    sections=(Section("T", 1, 6, 9, "section", "body"),),
    links=(Link("x.md", 2),),
    notes=("a note",),
)


def test_facts_round_trip_and_miss_on_another_version_or_language(conn):
    gs.put_facts(conn, "b1", "python", "1", FACTS)
    assert gs.facts(conn, "b1", "python", "1") == FACTS
    assert gs.facts(conn, "b1", "python", "2") is None
    assert gs.facts(conn, "b1", "tsx", "1") is None
    assert gs.facts(conn, "other", "python", "1") is None


def test_stored_facts_that_fail_the_check_read_as_a_miss(conn):
    gs.put_facts(conn, "b1", "python", "1", FACTS)
    conn.execute("UPDATE facts SET json = ?", ('{"lang": "python"}',))
    assert gs.facts(conn, "b1", "python", "1") is None
    conn.execute("UPDATE facts SET json = 'not json'")
    assert gs.facts(conn, "b1", "python", "1") is None
    gs.put_facts(conn, "b1", "python", "1", FACTS)
    conn.execute("UPDATE facts SET lines = 2")  # a line past the end of the file
    assert gs.facts(conn, "b1", "python", "1") is None
    conn.execute("UPDATE facts SET lines = 'many'")
    assert gs.facts(conn, "b1", "python", "1") is None


def test_facts_with_nothing_in_them_round_trip(conn):
    empty = Facts(lang="markdown")
    gs.put_facts(conn, "b", "markdown", "1", empty)
    assert gs.facts(conn, "b", "markdown", "1") == empty


def test_facts_can_be_read_on_a_read_only_connection(store):
    build(store, lambda c: gs.put_facts(c, "b1", "python", "1", FACTS))
    reader = gs.open_read(store)
    try:
        assert gs.facts(reader, "b1", "python", "1") == FACTS
    finally:
        reader.close()


def test_prune_keeps_tree_blobs_and_the_most_recently_seen_others(conn, monkeypatch):
    monkeypatch.setattr(gs, "FACTS_KEEP", 3)
    gs.put_file(conn, row("a.py", blob="tree1", state="indexed"))
    gs.put_file(conn, row("b.py", blob="tree2", state="unparsed"))
    for blob in ("tree1", "tree2", "o1", "o2", "o3", "o4", "o5"):
        gs.put_facts(conn, blob, "python", "1", FACTS)
    gs.facts(conn, "o1", "python", "1")  # o1 is used again: it is now the most recently seen
    assert gs.prune_facts(conn) == 2
    kept = {r[0] for r in conn.execute("SELECT blob FROM facts")}
    assert kept == {"tree1", "tree2", "o1", "o5", "o4"}
    assert gs.prune_facts(conn) == 0


def test_prune_keeps_every_tree_blob_even_past_the_limit(conn, monkeypatch):
    monkeypatch.setattr(gs, "FACTS_KEEP", 0)
    for i in range(5):
        gs.put_file(conn, row(f"{i}.py", blob=f"t{i}", state="indexed"))
        gs.put_facts(conn, f"t{i}", "python", "1", FACTS)
    gs.put_facts(conn, "gone", "python", "1", FACTS)
    assert gs.prune_facts(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 5


@pytest.mark.parametrize("state", ["ignored", "secret", "vendored", "excluded", "symlink", "submodule"])
def test_prune_drops_the_facts_of_a_blob_only_left_out_rows_name(conn, monkeypatch, state):
    """A path now left out keeps none of its extracted text, whatever `FACTS_KEEP` allows for orphans."""
    monkeypatch.setattr(gs, "FACTS_KEEP", 100)
    gs.put_file(conn, row("gone.py", blob="left", state=state))
    gs.put_file(conn, row("both.py", blob="shared", state=state))
    gs.put_file(conn, row("kept.py", blob="shared", state="indexed"))  # the same bytes, indexed at another path
    for blob in ("left", "shared", "orphan"):
        gs.put_facts(conn, blob, "python", "1", FACTS)
    assert gs.prune_facts(conn) == 1
    assert {r[0] for r in conn.execute("SELECT blob FROM facts")} == {"shared", "orphan"}


# --- search ---


@pytest.fixture
def searchable(conn):
    file_cap = node("agentflow/run_cap.py", text="")
    sym_cap = node("agentflow/run_cap.py", "RunCapHold", "symbol", text="holds the run cap")
    gov = node("agentflow/governor.py", text="holds runs on usage windows")
    gs.replace_graph(conn, "agentflow/run_cap.py", [file_cap, sym_cap], [])
    gs.replace_graph(conn, "agentflow/governor.py", [gov], [])
    return file_cap, sym_cap, gov


def test_search_finds_file_and_camel_case_names_before_the_unrelated_one(conn, searchable):
    file_cap, sym_cap, gov = searchable
    ids = [h.node.id for h in gs.search(conn, "run cap")]
    assert set(ids[:2]) == {file_cap.id, sym_cap.id}
    assert gov.id not in ids[:2]
    assert [h.node.id for h in gs.search(conn, "RunCap")][:2] == ids[:2]
    assert [h.node.id for h in gs.search(conn, "usage window")] == [gov.id]


def test_search_scores_rank_lowest_first_with_a_stable_tiebreak(conn, searchable):
    hits = gs.search(conn, "run cap")
    assert [h.score for h in hits] == sorted(h.score for h in hits)
    assert hits == gs.search(conn, "run cap")
    for i in range(3):
        gs.replace_graph(conn, f"t{i}.py", [node(f"t{i}.py", text="tie")], [])
    tied = [h.node.id for h in gs.search(conn, "tie")]
    assert tied == sorted(tied)


def test_search_kind_filters_and_limit_holds(conn, searchable):
    _, sym_cap, _ = searchable
    assert [h.node.id for h in gs.search(conn, "run cap", kind="symbol")] == [sym_cap.id]
    assert gs.search(conn, "run cap", kind="doc") == []
    assert len(gs.search(conn, "run cap", limit=1)) == 1
    assert gs.search(conn, "run cap", limit=0) == []
    assert gs.search(conn, "run cap", limit=-3) == []


def test_search_returns_the_stored_node(conn, searchable):
    _, sym_cap, _ = searchable
    assert gs.search(conn, "RunCapHold", kind="symbol")[0].node == sym_cap


def test_search_words_split_names_at_camel_case_and_separators(conn):
    n = node("a.py", "HTTPServerConfig_v2.run-now/x", "symbol")
    gs.replace_graph(conn, "a.py", [n], [])
    assert conn.execute("SELECT words FROM search").fetchone()[0] == "http server config v2 run now x"
    assert gs._tokens("RunCapHold") == ["run", "cap", "hold"]
    assert gs._tokens("run_cap.py") == ["run", "cap", "py"]


def test_search_uses_at_most_query_tokens_words(conn):
    gs.replace_graph(conn, "a.py", [node("a.py", text="zzz")], [])
    many = " ".join(f"w{i}" for i in range(gs.QUERY_TOKENS)) + " zzz"
    assert gs.search(conn, many) == []  # "zzz" is the 17th word, past the cap
    assert len(gs.search(conn, "zzz " + many)) == 1


HOSTILE = ["", '"', "* OR NEAR(", "a" * 5000, "'; DROP TABLE nodes; --", "\x00", 'run" OR "x', "AND OR NOT", "col:run", "run*", "^run", "(", "run -cap"]


@pytest.mark.parametrize("query", HOSTILE)
def test_hostile_queries_never_raise_and_change_nothing(conn, searchable, query):
    before = gs.counts(conn)
    dumped = gs.dump(conn)
    hits = gs.search(conn, query)
    assert isinstance(hits, list)
    assert gs.counts(conn) == before and gs.dump(conn) == dumped
    assert gs.search(conn, query, kind="symbol'; --") == []


def test_search_works_on_a_read_only_connection(store):
    a = node("run_cap.py", text="holds")
    build(store, lambda c: gs.replace_graph(c, "run_cap.py", [a], []))
    reader = gs.open_read(store)
    try:
        assert [h.node.id for h in gs.search(reader, "run cap")] == [a.id]
    finally:
        reader.close()


# --- neighbours ---


def chain(conn, size):
    nodes = [node(f"n{i}.py") for i in range(size)]
    for i, n in enumerate(nodes):
        edges = [edge(n, nodes[i + 1], n.path)] if i + 1 < size else []
        gs.replace_graph(conn, n.path, [n], edges)
    return nodes


def test_neighbours_walk_both_directions_to_the_depth(conn):
    n = chain(conn, 6)
    one = gs.neighbours(conn, n[2].id)
    assert [x.id for _, x in one] == [n[1].id, n[3].id]
    assert all(isinstance(e, Edge) for e, _ in one)
    two = gs.neighbours(conn, n[2].id, depth=2)
    assert {x.id for _, x in two} == {n[0].id, n[1].id, n[3].id, n[4].id}
    assert [x.id for _, x in two][:2] == [n[1].id, n[3].id]  # the nearer level first
    assert len(gs.neighbours(conn, n[0].id, depth=3)) == 3


def test_neighbours_depth_is_clamped_to_one_through_three(conn):
    n = chain(conn, 8)
    assert gs.neighbours(conn, n[0].id, depth=0) == gs.neighbours(conn, n[0].id, depth=1)
    assert len(gs.neighbours(conn, n[0].id, depth=5)) == 3
    assert gs.neighbours(conn, n[0].id, depth=5) == gs.neighbours(conn, n[0].id, depth=3)


def test_neighbours_return_at_most_200_rows_and_are_deterministic(conn):
    hub = node("hub.py")
    spokes = [node(f"s{i:03}.py") for i in range(260)]
    gs.replace_graph(conn, "hub.py", [hub], [edge(hub, s, "hub.py") for s in spokes])
    for s in spokes:
        gs.replace_graph(conn, s.path, [s], [edge(s, hub, s.path, "references")])
    rows = gs.neighbours(conn, hub.id, depth=5)
    assert len(rows) == 200
    assert rows == gs.neighbours(conn, hub.id, depth=5)


def test_neighbours_skip_dangling_edges_and_unknown_nodes(conn):
    a, ghost = node("a.py"), node("ghost.py")
    gs.replace_graph(conn, "a.py", [a], [edge(a, ghost, "a.py")])
    assert gs.neighbours(conn, a.id) == []
    assert gs.neighbours(conn, "nope") == []


def test_neighbours_do_not_loop_on_cycles(conn):
    a, b = node("a.py"), node("b.py")
    gs.replace_graph(conn, "a.py", [a], [edge(a, b, "a.py")])
    gs.replace_graph(conn, "b.py", [b], [edge(b, a, "b.py")])
    assert len(gs.neighbours(conn, a.id, depth=3)) == 2


# --- dump ---


def test_dump_is_the_same_whatever_order_the_store_was_built_in(tmp_path):
    a, b = node("a.py"), node("b.py")
    a_sym = node("a.py", "Hold", "symbol", text="holds")

    def fill(order):
        path = gs.path_for(tmp_path / f"s{len(order)}{order[0]}")
        conn, temp = gs.begin(path)
        for p in order:
            gs.put_file(conn, row(p))
            if p == "a.py":
                gs.replace_graph(conn, p, [a, a_sym], [edge(a, b, p)])
                gs.set_keys(conn, p, ["py:a"], ["py:b"])
            else:
                gs.replace_graph(conn, p, [b], [])
                gs.set_keys(conn, p, ["py:b"], [])
        gs.set_meta(conn, built_at=str(order))
        out = gs.dump(conn)
        gs.discard(conn, temp)
        return out

    first, second = fill(["a.py", "b.py"]), fill(["b.py", "a.py"])
    first["meta"], second["meta"] = [], []
    assert first == second
    assert set(first) == {"meta", "files", "facts", "provides", "wants", "nodes", "edges", "ignore_files", "search"}
    assert len(first["search"]) == 3


def test_dump_holds_facts_under_their_own_key_without_a_counter(conn):
    gs.put_facts(conn, "b1", "python", "1", FACTS)
    gs.put_facts(conn, "b2", "python", "1", FACTS)
    rows = gs.dump(conn)["facts"]
    assert [r[0] for r in rows] == ["b1", "b2"]
    assert all(len(r) == 5 for r in rows)


# --- scale: nothing a build does per file may scan a whole table ---


def test_the_statements_a_build_runs_per_file_use_an_index(conn):
    plans = {
        "max_seen": "SELECT MAX(seen) FROM facts",
        "facts_hit": "UPDATE facts SET seen = 1 WHERE blob = 'b' AND lang = 'l' AND version = 'v'",
        "facts_row": "SELECT json, lines FROM facts WHERE blob = 'b' AND lang = 'l' AND version = 'v'",
        "node_blob": "UPDATE nodes SET blob = 'b' WHERE path = 'p'",
        "keys_provides": "DELETE FROM provides WHERE path = 'p'",
        "keys_wants": "DELETE FROM wants WHERE path = 'p'",
        "drop_search": "DELETE FROM search WHERE rowid IN (SELECT rowid FROM nodes WHERE path = 'p')",
        "drop_nodes": "DELETE FROM nodes WHERE path = 'p'",
        "drop_edges": "DELETE FROM edges WHERE origin = 'p'",
        "drop_file": "DELETE FROM files WHERE path = 'p'",
        "node_by_id": "SELECT rowid FROM nodes WHERE id = 'i'",
        "file_blob": "SELECT blob FROM files WHERE path = 'p'",
        "providers": "SELECT path FROM provides WHERE key = 'k' ORDER BY path",
        "wanting": "SELECT path FROM wants WHERE key IN ('a', 'b')",
        "edges_src": "SELECT src FROM edges WHERE src = 'n' ORDER BY src, dst, kind, origin",
        "edges_dst": "SELECT src FROM edges WHERE dst = 'n' ORDER BY src, dst, kind, origin",
        "nodes_at": "SELECT id FROM nodes WHERE path = 'p' ORDER BY line, id",
    }
    for name, sql in plans.items():
        detail = " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql))
        detail = detail.replace("SCAN search VIRTUAL TABLE INDEX 0:=", "FTS5 rowid lookup")  # `0:=` is a rowid match
        assert "SCAN" not in detail, (name, detail)
        if name == "max_seen":  # a bare `SEARCH facts` is also what an unindexed MAX reports
            assert "facts_seen" in detail, detail


def test_a_build_with_many_cached_facts_is_not_quadratic(conn):
    import time

    count = 10000
    started = time.monotonic()
    for i in range(count):
        gs.put_facts(conn, f"blob{i}", "python", "1", FACTS)
    for i in range(count):
        assert gs.facts(conn, f"blob{i}", "python", "1") is not None
    gs.prune_facts(conn)
    # Linear this takes about a second; a scan of the facts table per call took 8 s here and minutes with real facts.
    assert time.monotonic() - started < 5


def test_begin_closes_the_source_when_it_cannot_make_the_temp_file(store, monkeypatch):
    build(store)
    opened = []
    real_open = gs.open_read

    def spy(path, **kw):
        opened.append(real_open(path, **kw))
        return opened[-1]

    def refuse(path):
        raise OSError("no room")

    monkeypatch.setattr(gs, "open_read", spy)
    monkeypatch.setattr(gs, "_create_temp", refuse)
    with pytest.raises(OSError):
        gs.begin(store)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")
