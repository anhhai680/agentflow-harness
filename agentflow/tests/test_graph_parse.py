"""graph_parse and graph_worker: the parser runs in a limited child process, and nothing it says is trusted."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentflow import graph_model as gm
from agentflow import graph_parse, graph_worker
from agentflow.graph_parse import Parser, worker_argv, worker_env

BAD = Path(__file__).parent / "fixtures" / "graph" / "bad_worker.py"


@pytest.fixture
def parser():
    p = Parser(download=False, argv=worker_argv("stdlib"))
    yield p
    p.close()


def _bad(behaviour: str, log: Path) -> Parser:
    return Parser(download=False, argv=[sys.executable, str(BAD), behaviour, str(log)])


def _starts(log: Path) -> int:
    return len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0


def _group_gone(pgid: int, within: float = 3.0) -> bool:
    deadline = time.monotonic() + within
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)


# --- the real worker ---


def test_stdlib_worker_parses_python_markdown_and_manifests(parser):
    assert parser.mode == "stdlib" and set(parser.languages) == {"python", "markdown", "manifest"}
    assert [d.name for d in parser.parse("a.py", "python", b"def f(): pass\n").defs] == ["f"]
    assert parser.parse("a.md", "markdown", b"# T\n").doc == "T"
    assert parser.parse("package.json", "manifest", b'{"name": "x"}').package == "x"


def test_the_handshake_names_each_extractor_version(parser):
    assert parser.languages == {lang: gm.EXTRACTOR_VERSIONS[lang] for lang in ("python", "markdown", "manifest")}


def test_a_language_without_a_grammar_is_a_note(parser):
    assert parser.parse("a.ts", "typescript", b"export const a = 1").notes == ("no_grammar",)


def test_the_stdlib_worker_imports_no_third_party_package(tmp_path):
    # `-I -S` gives it no site-packages: a third-party import would stop the handshake
    out = subprocess.run(worker_argv("stdlib"), input=b"", capture_output=True, cwd=tmp_path, env=worker_env(), timeout=30)
    assert json.loads(out.stdout.splitlines()[0])["ready"] == 1


def test_the_worker_writes_only_protocol_lines(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    req = json.dumps({"id": 7, "path": "a.py", "lang": "python", "size": 14}).encode() + b"\n" + b"def f(): pass\n"
    out = subprocess.run(worker_argv("stdlib"), input=req, capture_output=True, cwd=cwd, env=worker_env(), timeout=30)
    lines = out.stdout.splitlines()
    assert len(lines) == 2 and out.stdout.endswith(b"\n") and out.returncode == 0
    reply = json.loads(lines[1])
    assert reply["id"] == 7 and gm.check_facts(reply["facts"], "python", 2).defs[0].name == "f"
    assert list(cwd.iterdir()) == []  # it wrote no file in its directory


@pytest.mark.parametrize("head", [
    {"id": 1, "path": "a.py", "lang": "python", "size": graph_worker.DATA_MAX_BYTES + 1},
    {"id": 1, "path": "a.py", "lang": "python", "size": -1},
    {"id": 1, "path": "a.py", "lang": "python", "size": 5, "extra": 1},
    {"id": True, "path": "a.py", "lang": "python", "size": 5},
    "not a mapping",
])
def test_the_worker_exits_on_a_request_it_does_not_accept(tmp_path, head):
    req = json.dumps(head).encode() + b"\n" + b"x = 1"
    out = subprocess.run(worker_argv("stdlib"), input=req, capture_output=True, cwd=tmp_path, env=worker_env(), timeout=30)
    assert len(out.stdout.splitlines()) == 1 and out.returncode != 0


def test_the_worker_exits_when_the_bytes_stop_short(tmp_path):
    req = json.dumps({"id": 1, "path": "a.py", "lang": "python", "size": 100}).encode() + b"\n" + b"x = 1"
    out = subprocess.run(worker_argv("stdlib"), input=req, capture_output=True, cwd=tmp_path, env=worker_env(), timeout=30)
    assert len(out.stdout.splitlines()) == 1 and out.returncode != 0


def _serve(extractors: dict, lang: str = "python", data: bytes = b"x = 1\n") -> dict:
    inp = io.BytesIO(json.dumps({"id": 3, "path": "a.py", "lang": lang, "size": len(data)}).encode() + b"\n" + data)
    out = io.BytesIO()
    assert graph_worker.serve(inp, out, extractors) == 0
    return json.loads(out.getvalue().splitlines()[0])


def test_an_extractor_that_raises_is_a_worker_error_note():
    def boom(path: str, data: bytes) -> gm.Facts:
        raise RuntimeError("parser bug")
    reply = _serve({"python": boom})
    assert reply["id"] == 3 and reply["facts"]["notes"] == ["worker_error"] and reply["facts"]["lang"] == "python"


def test_a_reply_over_the_cap_is_replaced_by_a_note():
    big = gm.Facts(lang="markdown", sections=tuple(
        gm.Section("t", 1, 1, 1, "section", "x" * gm.SECTION_TEXT_MAX) for _ in range(gm.MAX_SECTIONS)))
    reply = _serve({"markdown": lambda path, data: big}, lang="markdown")
    assert reply["facts"]["notes"] == ["reply_too_large"] and reply["facts"]["sections"] == []
    assert graph_worker.REPLY_MAX_BYTES == graph_parse.REPLY_MAX_BYTES
    assert graph_worker.DATA_MAX_BYTES == graph_parse.DATA_MAX_BYTES


def test_deep_nesting_costs_a_note_and_the_next_parse_still_works(parser):
    deep = parser.parse("deep.py", "python", b"(" * 200000)
    assert deep.notes and deep.defs == ()
    assert [d.name for d in parser.parse("a.py", "python", b"def g(): pass\n").defs] == ["g"]


def test_a_file_over_the_request_cap_is_never_sent(parser, monkeypatch):
    monkeypatch.setattr(graph_parse, "DATA_MAX_BYTES", 10)
    assert parser.parse("a.py", "python", b"x = 1\n" * 10).notes == ("too_large",)
    assert parser.parse("a.py", "python", b"x = 1\n").notes == ()


def test_memory_over_the_limit_kills_the_worker(parser, monkeypatch):
    if graph_parse.rss_mb(parser._worker.pid, parser._worker.proc.pid) is None:
        pytest.skip("ps reports no resident size for the worker here")
    monkeypatch.setattr(graph_parse, "RSS_MAX_MB", 1)
    src = b"".join(b"def f%d(a, b):\n    return a + b\n" % i for i in range(6000))
    assert len(src) >= 200_000
    pgid = parser._worker.proc.pid
    assert parser.parse("big.py", "python", src).notes == ("parser_memory",)
    assert _group_gone(pgid)


def test_close_leaves_no_child_and_no_temp_directory():
    p = Parser(download=False, argv=worker_argv("stdlib"))
    pgid, cwd = p._worker.proc.pid, Path(p._worker.cwd)
    assert cwd.is_dir() and list(cwd.iterdir()) == []
    p.close()
    assert _group_gone(pgid) and not cwd.exists()
    p.close()  # idempotent
    assert p.parse("a.py", "python", b"x = 1\n").notes == ("parser_unavailable",)


def test_the_parser_is_a_context_manager():
    with Parser(download=False, argv=worker_argv("stdlib")) as p:
        pgid = p._worker.proc.pid
        assert p.parse("a.py", "python", b"def f(): pass\n").defs[0].name == "f"
    assert _group_gone(pgid)


def test_the_worker_runs_from_an_empty_directory_with_no_import_path(parser):
    # its cwd is an empty temp dir and its env has no PYTHONPATH: it found `agentflow` beside itself
    assert parser._worker.cwd != os.getcwd() and "PYTHONPATH" not in worker_env()


# --- a worker that misbehaves ---


@pytest.mark.parametrize("behaviour", ["hang", "half"])
def test_a_worker_that_goes_silent_costs_one_timeout_note(behaviour, tmp_path, monkeypatch):
    monkeypatch.setattr(graph_parse, "PARSE_TIMEOUT_S", 1)
    p = _bad(behaviour, tmp_path / "starts")
    try:
        pgid = p._worker.proc.pid
        t0 = time.monotonic()
        assert p.parse("a.py", "python", b"x = 1\n").notes == ("parser_timeout",)
        assert time.monotonic() - t0 < 3
        assert _group_gone(pgid)
        assert p.parse("a.py", "python", b"x = 1\n").notes == ("parser_timeout",)
        assert _starts(tmp_path / "starts") == 2
    finally:
        p.close()


@pytest.mark.parametrize(("behaviour", "note"), [
    ("huge", "parser_reply"), ("wrong_id", "parser_reply"), ("other_path", "parser_reply"),
    ("past_end", "parser_reply"), ("not_json", "parser_reply"), ("exit", "parser_crashed"),
])
def test_a_reply_that_is_not_as_asked_costs_one_note_and_a_fresh_worker(behaviour, note, tmp_path):
    p = _bad(behaviour, tmp_path / "starts")
    try:
        pgid = p._worker.proc.pid
        assert p.parse("a.py", "python", b"x = 1\n").notes == (note,)
        assert _group_gone(pgid)
        assert p.parse("a.py", "python", b"x = 1\n").notes == (note,)
        assert _starts(tmp_path / "starts") == 2
    finally:
        p.close()


def test_after_max_restarts_the_parser_is_unavailable_and_starts_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(graph_parse, "MAX_RESTARTS", 2)
    log = tmp_path / "starts"
    p = _bad("exit", log)
    try:
        notes = [p.parse("a.py", "python", b"x = 1\n").notes for _ in range(5)]
        assert notes == [("parser_crashed",)] * 3 + [("parser_unavailable",)] * 2
        assert _starts(log) == 3
        assert p.parse("a.ts", "typescript", b"").notes == ("no_grammar",)
    finally:
        p.close()


def test_an_override_with_no_handshake_is_never_retried(tmp_path):
    log = tmp_path / "starts"
    t0 = time.monotonic()
    p = _bad("no_handshake", log)
    try:
        assert p.languages == {} and p.mode == ""
        assert [p.parse("a.py", "python", b"x = 1\n").notes for _ in range(3)] == [("no_grammar",)] * 3
        assert time.monotonic() - t0 < 5 and _starts(log) == 1
    finally:
        p.close()


@pytest.mark.parametrize("hello", [
    {"ready": 1, "pid": 1, "languages": {"python": "1"}},
    {"ready": 1, "pid": True, "languages": {"python": "1"}},
    {"ready": 2, "pid": 4242, "languages": {"python": "1"}},
    {"ready": 1, "pid": 4242, "languages": {"python": "1"}, "more": 1},
    ["ready"],
])
def test_a_handshake_that_is_not_as_it_must_be_is_refused(hello):
    assert graph_parse._ready(json.dumps(hello).encode() + b"\n") is None


@pytest.mark.parametrize("languages", [
    ["python"], {"python": 1}, {"cobol": "1"}, {"python": "1" * 41}, {"python": "1\n"}, {"python": "1", "rust": "1"},
])
def test_handshake_languages_that_are_odd_are_no_languages(languages):
    assert graph_parse._ready(json.dumps({"ready": 1, "pid": 4242, "languages": languages}).encode()) == (4242, {})


def test_handshake_languages_that_are_known_are_kept():
    hello = {"ready": 1, "pid": 4242, "languages": {"python": "1", "typescript": "1+0.23.2"}}
    assert graph_parse._ready(json.dumps(hello).encode()) == (4242, {"python": "1", "typescript": "1+0.23.2"})


# --- starting it ---


def test_worker_env_carries_nothing_else(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/evil")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    assert set(worker_env()) <= {"PATH", "HOME", "LANG", "UV_CACHE_DIR", "XDG_CACHE_HOME"}


def test_worker_env_keeps_the_uv_cache_and_a_plain_locale(monkeypatch):
    monkeypatch.setenv("UV_CACHE_DIR", "/c/uv")
    monkeypatch.setenv("XDG_CACHE_HOME", "/c")
    monkeypatch.setenv("LANG", "de_DE.ISO-8859-1")
    monkeypatch.setenv("LC_ALL", "de_DE")
    env = worker_env()
    assert env["UV_CACHE_DIR"] == "/c/uv" and env["XDG_CACHE_HOME"] == "/c" and env["LANG"] == "C.UTF-8"
    assert "LC_ALL" not in env and env["PATH"] == os.environ["PATH"]


def test_worker_argv_for_each_mode(monkeypatch, tmp_path):
    assert worker_argv("stdlib") == [sys.executable, "-I", "-S", str(graph_parse.WORKER)]
    fake = tmp_path / "uv"
    fake.write_text("#!/bin/sh\nexit 3\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    tail = ["--python", sys.executable, "--script", str(graph_parse.WORKER)]
    assert worker_argv("locked") == [str(fake), "run", "--quiet", "--locked", *tail]
    assert worker_argv("offline") == [str(fake), "run", "--quiet", "--locked", "--offline", *tail]
    monkeypatch.setenv("PATH", str(tmp_path / "none"))
    assert worker_argv("locked") is None and worker_argv("offline") is None


def test_without_uv_on_path_the_parser_falls_back_to_stdlib(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with Parser(download=False) as p:
        assert p.mode == "stdlib" and "python" in p.languages


@pytest.mark.parametrize("download", [False, True])
def test_a_uv_mode_that_exits_at_once_falls_back_to_stdlib_quickly(monkeypatch, tmp_path, download):
    fake = tmp_path / "uv"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    t0 = time.monotonic()
    with Parser(download=download) as p:
        assert p.mode == "stdlib" and time.monotonic() - t0 < 10
        assert p.parse("a.py", "python", b"def f(): pass\n").defs[0].name == "f"


# a stand-in for `uv run --script`: it starts python as its own child, the way uv does, so the worker is a grandchild
FAKE_UV = """#!/bin/sh
while [ $# -gt 1 ]; do
  case "$1" in --python) py="$2"; shift;; --script) script="$2"; shift;; esac
  shift
done
"$py" -I -S "$script"
exit $?
"""


def test_a_uv_mode_runs_the_worker_as_a_grandchild_and_kills_its_whole_group(monkeypatch, tmp_path):
    fake = tmp_path / "uv"
    fake.write_text(FAKE_UV)
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    p = Parser(download=False)
    try:
        assert p.mode == "offline" and set(p.languages) == {"python", "markdown", "manifest"}
        pgid, pid = p._worker.proc.pid, p._worker.pid
        assert pid != pgid  # the handshake names the worker, not the uv in front of it
        assert p.parse("a.py", "python", b"def f(): pass\n").defs[0].name == "f"
    finally:
        p.close()
    assert _group_gone(pgid)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
