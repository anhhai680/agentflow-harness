# /// script
# requires-python = ">=3.12"
# dependencies = ["tree-sitter==0.27.0", "tree-sitter-javascript==0.25.0", "tree-sitter-typescript==0.23.2"]
# ///
"""The parser process: it reads one file's bytes at a time on stdin and answers with that file's facts as JSON.

`graph_parse.Parser` starts this script as a child, limits it and distrusts every word it says. The protocol is lines
of JSON on stdin and stdout. The worker writes one handshake line first, `{"ready": 1, "pid": <its own pid>,
"languages": {<lang>: <extractor version>}}`. Each request is one line `{"id": n, "path": str, "lang": str, "size": n}`
followed by exactly `size` bytes, and each reply is one line `{"id": n, "facts": {...}}` of at most `REPLY_MAX_BYTES`
(a larger one is replaced by the note `reply_too_large`). A request that is not as it must be, or bytes that stop short,
end the worker. An extractor that raises gives the note `worker_error`, never a dead worker.

It runs as a script (`python -I -S graph_worker.py` or `uv run --script`), so before it imports anything else it
takes its own directory off `sys.path` and appends the directory above it: `agentflow.*` resolves to the modules
beside it, and nothing there can shadow the standard library. Only `os` and `sys` are imported at the top: under `-I`
the script's directory is never on `sys.path`, and under `uv run` `site` has loaded `os` before the script starts;
every other import waits for `_import_root`. It sets `RLIMIT_AS` to 1 GiB where the system
allows (macOS refuses), writes no bytecode, opens no file and no socket, and keeps file descriptor 1 for protocol lines
only: anything else written to stdout goes to stderr, which the parent sends to /dev/null.

It imports `graph_model` and the extractors and nothing else from the engine, and never `yaml`. The script header above
names the exact tree-sitter packages, and `graph_worker.py.lock` beside it holds their hashes: under `uv run --locked
--script` the worker also offers `graph_syntax.available()` (TypeScript, TSX and JavaScript, each versioned
`<extractor>+<grammar>`); under `python -I -S` that is `{}` and it offers the stdlib extractors only.
"""

from __future__ import annotations

import os
import sys

REQUEST_LINE_MAX = 1024 * 1024
DATA_MAX_BYTES = 16 * 1024 * 1024  # graph_parse.DATA_MAX_BYTES: the parent never sends more
REPLY_MAX_BYTES = 4 * 1024 * 1024  # graph_parse.REPLY_MAX_BYTES, newline included
ADDRESS_SPACE = 1024 * 1024 * 1024
_REQUEST_KEYS = frozenset(("id", "path", "lang", "size"))


def _import_root() -> str:
    """Take this script's directory off `sys.path` and append its parent; return this directory."""
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != here]
    sys.path.append(os.path.dirname(here))
    return here


def _limit_memory() -> None:
    import resource  # after `_import_root`: see the module docstring

    try:
        resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE, ADDRESS_SPACE))
    except (ValueError, OSError):  # macOS refuses; the parent's resident-size watchdog covers it there
        pass


def _int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _request(line: bytes) -> dict | None:
    import json

    if not line.endswith(b"\n"):
        return None
    try:
        req = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(req, dict) or set(req) != _REQUEST_KEYS:
        return None
    if not _int(req["id"]) or not isinstance(req["path"], str) or not isinstance(req["lang"], str):
        return None
    if not _int(req["size"]) or not 0 <= req["size"] <= DATA_MAX_BYTES:
        return None
    return req


def _line(message: dict) -> bytes:
    import json

    return json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"


def _answer(req: dict, data: bytes, extractors: dict) -> bytes:
    """The reply line for one request: its facts, or a note when the extractor raised or the reply is too large."""
    from agentflow.graph_model import empty, to_json

    lang, rid = req["lang"], req["id"]
    try:
        extract = extractors.get(lang)
        facts = extract(req["path"], data) if extract is not None else empty(lang, "no_grammar")
        line = _line({"id": rid, "facts": to_json(facts)})
    except Exception:  # an extractor bug is one note for this file, never a dead worker
        return _line({"id": rid, "facts": to_json(empty(lang, "worker_error"))})
    if len(line) > REPLY_MAX_BYTES:
        return _line({"id": rid, "facts": to_json(empty(lang, "reply_too_large"))})
    return line


def serve(inp, out, extractors: dict) -> int:
    """Answer requests from `inp` on `out` until end of input (0) or a request that is not as it must be (1)."""
    while True:
        head = inp.readline(REQUEST_LINE_MAX + 1)
        if not head:
            return 0
        req = _request(head) if len(head) <= REQUEST_LINE_MAX else None
        if req is None:
            return 1
        data = inp.read(req["size"])
        if len(data) != req["size"]:
            return 1
        out.write(_answer(req, data, extractors))
        out.flush()


def main() -> int:
    here = _import_root()
    sys.dont_write_bytecode = True
    _limit_memory()
    import warnings

    warnings.simplefilter("ignore")
    proto = os.fdopen(os.dup(1), "wb")  # protocol lines only; fd 1 now points at stderr (/dev/null)
    try:
        os.dup2(2, 1)
    except OSError:
        pass
    sys.stdout = sys.stderr
    import functools

    from agentflow import graph_manifest, graph_markdown, graph_model, graph_python, graph_syntax

    if os.path.dirname(os.path.abspath(graph_model.__file__)) != here:
        return 2  # some other `agentflow` came first: answer nothing rather than run code that is not ours
    extractors = {"python": graph_python.extract, "markdown": graph_markdown.extract, "manifest": graph_manifest.extract}
    languages = {lang: graph_model.EXTRACTOR_VERSIONS[lang] for lang in extractors}
    for lang, version in graph_syntax.available().items():  # `{}` in the stdlib mode, which has no tree-sitter
        extractors[lang] = functools.partial(graph_syntax.extract, lang=lang)
        languages[lang] = version
    proto.write(_line({"ready": 1, "pid": os.getpid(), "languages": languages}))
    proto.flush()
    return serve(sys.stdin.buffer, proto, extractors)


if __name__ == "__main__":
    sys.exit(main())
