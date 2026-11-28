"""A scripted stand-in for `graph_worker.py` that misbehaves as its first argument says.

`hang` (handshake, then sleep), `half` (half a reply, then sleep), `huge` (one 5 MB line), `wrong_id`, `other_path`
(valid facts whose `lang` is another language), `past_end` (a definition below the file's last line), `not_json`,
`exit` (handshake, then exit), `no_handshake` (exit before a word), `flood` (handshake, then lines just under the
reply cap, unasked and without end), `extra` (a valid reply, then one more line), `die_idle` (a valid reply, then exit)
and `no_languages` (a handshake that offers only an unknown language, then valid replies). A second argument names a file that gets one
line per start, so a test can count the processes the parser started.
"""

from __future__ import annotations

import json
import os
import sys
import time

FACT_KEYS = ("doc", "defs", "imports", "refs", "sections", "links", "deps", "package", "ecosystem", "status", "notes")


def facts(lang: str, **fields: object) -> dict:
    out: dict = {key: [] for key in FACT_KEYS}
    out.update(doc="", package="", ecosystem="", status="", lang=lang)
    out.update(fields)
    return out


def write(out, data: bytes) -> None:
    out.write(data)
    out.flush()


def main() -> int:
    behaviour = sys.argv[1]
    if len(sys.argv) > 2:
        with open(sys.argv[2], "a", encoding="utf-8") as log:
            log.write(behaviour + "\n")
    if behaviour == "no_handshake":
        return 0
    out, inp = sys.stdout.buffer, sys.stdin.buffer
    languages = {"cobol": "1"} if behaviour == "no_languages" else {"python": "1"}
    write(out, json.dumps({"ready": 1, "pid": os.getpid(), "languages": languages}).encode() + b"\n")
    if behaviour == "flood":
        line = b"x" * (4 * 1024 * 1024 - 1) + b"\n"
        while True:
            write(out, line)
    if behaviour == "exit":
        return 0
    if behaviour == "hang":
        time.sleep(60)
        return 0
    while True:
        line = inp.readline()
        if not line:
            return 0
        req = json.loads(line)
        inp.read(req["size"])
        rid, lang = req["id"], req["lang"]
        if behaviour == "half":
            write(out, b'{"id": %d, "fac' % rid)
            time.sleep(60)
        elif behaviour == "huge":
            write(out, b"x" * (5 * 1024 * 1024) + b"\n")
        elif behaviour == "not_json":
            write(out, b"this is not json\n")
        elif behaviour in ("extra", "die_idle", "no_languages"):
            write(out, json.dumps({"id": rid, "facts": facts(lang)}).encode() + b"\n")
            if behaviour == "extra":
                write(out, json.dumps({"id": rid + 1, "facts": facts(lang)}).encode() + b"\n")
            if behaviour == "die_idle":
                return 0
        else:
            reply = {
                "wrong_id": {"id": rid + 1, "facts": facts(lang)},
                "other_path": {"id": rid, "facts": facts("markdown" if lang != "markdown" else "python")},
                "past_end": {"id": rid, "facts": facts(lang, defs=[{
                    "name": "f", "kind": "function", "line": 99, "end": 99, "signature": "def f()", "doc": "",
                    "exported": True}])},
            }[behaviour]
            write(out, json.dumps(reply).encode() + b"\n")


if __name__ == "__main__":
    sys.exit(main())
