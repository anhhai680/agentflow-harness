"""graph_markdown: sections, decisions, questions, requirements, links and the Status line, and facts `check_facts` always accepts."""

from __future__ import annotations

import random

from agentflow import graph_model as gm
from agentflow.graph_markdown import extract

DOC = b"""# Proposal: a thing

Status: approved (2026-11-28)

## Design
Body text. See [the plan](../plans/p.md#task-1) and [site](https://example.com).

### Part
Inner.

```
## not a heading
```

## Decisions (grilling, 2026-11-28)
1. **One index.** Because.
2. **No embeddings.**

## Open Questions
- Is it fast?

**Resolved (2026-11-28, grilling)**
- **Q1 - Size.** Small.

## Success criteria
1. It builds.
"""


def _roundtrip(data: bytes) -> gm.Facts:
    """The facts for `data`, proven to pass `check_facts` unchanged."""
    facts = extract("docs/x.md", data)
    checked = gm.check_facts(gm.to_json(facts), "markdown", data.count(b"\n") + 1)
    assert checked == facts
    return facts


def _titles(f: gm.Facts, kind: str) -> list[str]:
    return [s.title for s in f.sections if s.kind == kind]


def test_markdown():
    f = _roundtrip(DOC)
    assert (f.doc, f.status) == ("Proposal: a thing", "approved (2026-11-28)")
    kinds = [(s.kind, s.title) for s in f.sections]
    assert ("section", "Design") in kinds and ("section", "Part") in kinds and ("section", "not a heading") not in kinds
    assert _titles(f, "decision") == ["One index. Because.", "No embeddings.", "Q1 - Size. Small."]
    assert _titles(f, "question") == ["Is it fast?"]
    assert _titles(f, "requirement") == ["It builds."]
    assert [link.target for link in f.links] == ["../plans/p.md"]
    design = next(s for s in f.sections if s.title == "Design")
    assert design.text.startswith("Body text.") and "Inner." not in design.text
    assert f.notes == ()


def test_spans():
    f = _roundtrip(DOC)
    lines = DOC.decode().split("\n")

    def line_of(prefix: str) -> int:
        return next(i for i, row in enumerate(lines, 1) if row.startswith(prefix))

    by_title = {s.title: s for s in f.sections}
    design, part, decisions = by_title["Design"], by_title["Part"], by_title["Decisions (grilling, 2026-11-28)"]
    assert (design.level, part.level, decisions.level) == (2, 3, 2)
    assert (design.line, design.end) == (line_of("## Design"), line_of("## Decisions") - 1)  # a sub-heading stays inside it
    assert (part.line, part.end) == (line_of("### Part"), line_of("## Decisions") - 1)
    assert by_title["Proposal: a thing"].end == len(lines)  # the one H1 runs to the last line
    item = by_title["One index. Because."]
    assert (item.line, item.end, item.level) == (line_of("1. **One"), line_of("1. **One"), 3)
    assert 1 <= min(s.line for s in f.sections) and max(s.end for s in f.sections) <= len(lines)


def test_item_continuation_and_gaps():
    data = b"## Decisions\n- First\n  continues here\nlazy line\n\n  indented after a blank\n\nnot part of it\n- Second\n  - nested\n\n## Next\n"
    f = _roundtrip(data)
    first, second = (s for s in f.sections if s.kind == "decision")
    assert (first.line, first.end) == (2, 6)
    assert first.title == "First" and "indented after a blank" in first.text and "not part" not in first.text
    assert (second.line, second.end, second.title) == (9, 10, "Second")


def test_fence_in_item_and_unclosed_fence():
    f = _roundtrip(b"## Decisions\n- A\n  ```\n  ## x\n\n  - y\n  ```\n- B\n")
    a, b = (s for s in f.sections if s.kind == "decision")
    assert (a.line, a.end, b.line) == (2, 7, 8)
    g = _roundtrip(b"## A\n~~~\n## hidden\n``` not a closer\n## B\n")
    assert [s.title for s in g.sections] == ["A"] and g.sections[0].end == 6  # the unclosed fence runs to the last line
    h = _roundtrip(b"## A\n```py\ncode\n````\n## B\n")  # a longer fence closes a shorter one
    assert [s.title for s in h.sections] == ["A", "B"]
    i = _roundtrip(b"## A\n````\n```\n## inner\n````\n## B\n")  # a shorter fence does not close a longer one
    assert [s.title for s in i.sections] == ["A", "B"]


def test_headings():
    f = _roundtrip(b"#hashtag\n#\n## Closed ##\n#### Deep  ###   \n####### seven\n    ## indented code\n\\## escaped\n### \n")
    assert [(s.level, s.title) for s in f.sections] == [(1, ""), (2, "Closed"), (4, "Deep"), (3, "")]
    assert f.doc == ""
    g = _roundtrip(b"# One\n# Two\n")
    assert g.doc == "One"


def test_modes_and_resolved():
    f = _roundtrip(
        b"## Open Questions\n- Q one\n### Sub\n- Q two\n\n**Resolved (x)**\n- Done one\n\n## Other\n- plain item\n\n**Resolved (y)**\n"
        b"- Done two\n\n## success CRITERIA\n- Req\n\n### Inner\n- Req two\n\n## Decisions\n- Dec\n\n## Next\n- not recorded\n"
    )
    assert _titles(f, "question") == ["Q one", "Q two"]
    assert _titles(f, "decision") == ["Done one", "Done two", "Dec"]
    assert _titles(f, "requirement") == ["Req", "Req two"]
    assert "plain item" not in [s.title for s in f.sections]
    g = _roundtrip(b"**Resolved**\n- Early\n# Title\n- not a decision\n")
    assert _titles(g, "decision") == ["Early"] and _titles(g, "section") == ["Title"]
    h = _roundtrip(b"## Decisions\n* * *\n- - -\n---\n- real\n-\n- \n")
    assert _titles(h, "decision") == ["real"]


def test_status():
    f = _roundtrip(b"# T\nStatus: proposed\nStatus: approved\n## A\n")
    assert f.status == "proposed"
    assert _roundtrip(b"# T\n## A\nStatus: approved\n").status == ""
    assert _roundtrip(b"# T\n```\nStatus: approved\n```\n").status == ""
    assert _roundtrip(b"Status:\n").status == ""
    assert _roundtrip(b"Status: " + b"x" * 500 + b"\n").status == "x" * gm.DOC_MAX


def test_links():
    data = (
        b"[a](./a.md) [b](../b.md#h) [c](c.md?x=1) [d](#top) [e](/abs) [f](mailto:x@y.z) [g](http://h/p) [h](//h/p) [i](?q)\n"
        b"[j](sp%20ace.md) [k](caf\xc3\xa9.md) [l](a/../b.md) [m]() [n](x.md \"title\") ![img](img/p.png)\n"
        b"`[code](code.md)` and [real](real.md)\n```\n[fenced](fenced.md)\n```\n"
    )
    f = _roundtrip(data)
    assert [(link.target, link.line) for link in f.links] == [
        ("./a.md", 1), ("../b.md", 1), ("c.md", 1), ("x.md", 2), ("img/p.png", 2), ("real.md", 3),
    ]  # fmt: skip
    assert len(f.notes) == 1 and f.notes[0].startswith("dropped 3 links")
    assert _roundtrip(b"[a](" + b"x" * 300 + b".md)").links == ()  # past 200 characters


def test_text_is_cut():
    f = _roundtrip(b"## Big\n" + b"word " * 400_000 + b"\n## Small\ntiny\n")
    big = next(s for s in f.sections if s.title == "Big")
    assert len(big.text) == gm.SECTION_TEXT_MAX
    assert next(s for s in f.sections if s.title == "Small").text == "tiny"
    g = _roundtrip(b"## " + b"t" * 5000 + b"\n")
    assert len(g.sections[0].title) == gm.NAME_MAX
    h = _roundtrip(b"## Decisions\n- " + b"d" * 5000 + b"\n")
    assert len(h.sections[1].title) == gm.NAME_MAX and len(h.sections[1].text) == gm.SECTION_TEXT_MAX


def test_many_sections_are_cut():
    f = _roundtrip(b"".join(b"## S%d\n" % i for i in range(3000)))
    assert len(f.sections) == gm.MAX_SECTIONS
    assert f.sections[-1].title == "S1999" and f.sections[0].end == 1
    assert f.notes == (f"cut sections to {gm.MAX_SECTIONS}",)
    g = _roundtrip(b"## Decisions\n" + b"- item\n" * 3000)
    assert len(g.sections) == gm.MAX_SECTIONS and len(g.notes) == 1
    h = _roundtrip(b"".join(b"[a](a%d.md)\n" % i for i in range(2500)))
    assert len(h.links) == gm.MAX_LINKS and h.notes == (f"cut links to {gm.MAX_LINKS}",)


def test_line_endings_and_bytes():
    crlf = _roundtrip(DOC.replace(b"\n", b"\r\n"))
    assert (crlf.doc, crlf.status) == ("Proposal: a thing", "approved (2026-11-28)")
    assert _titles(crlf, "decision") == ["One index. Because.", "No embeddings.", "Q1 - Size. Small."]
    assert [s.title for s in _roundtrip(b"\xef\xbb\xbf# Bom\n").sections] == ["Bom"]
    cr = _roundtrip(b"# A\r## B\r- c\r")  # a lone CR is no line break: one line
    assert [(s.title, s.line, s.end) for s in cr.sections] == [("A ## B - c", 1, 1)]
    for blank in (b"", b"\n", b"\n\n", b"   "):
        assert _roundtrip(blank) == gm.Facts(lang="markdown")
    assert _roundtrip(b"# no newline at end").sections[0].line == 1


def test_bad_utf8_is_one_note():
    assert extract("a.md", b"# ok\n\xff\xfe\n") == gm.empty("markdown", "UnicodeDecodeError")


def test_control_characters_are_cleaned():
    f = _roundtrip(b"# T\x00itle\x1b[2J\xe2\x80\xa8x\n\x07body\n")
    assert f.doc == "T itle [2J x" and f.sections[0].text == "body"


def test_hostile_lines_stay_fast():
    for line in (b"[" * 200_000, b"[a](" * 100_000, b"`" * 200_000, b"#" + b" " * 200_000 + b"x", b"- " * 100_000, b"[a" * 100_000 + b"](b)"):
        _roundtrip(b"## Decisions\n" + line + b"\n")


def test_random_documents_always_pass_the_check():
    rng = random.Random(7)
    pieces = [b"# ", b"## ", b"### ", b"Status: ", b"- ", b"1. ", b"* * *", b"```", b"~~~", b"**Resolved (x)**", b"## Decisions", b"## Open Questions",
              b"[a](b.md)", b"[a](../x#y)", b"[](", b"](", b"`", b"\n", b"\n", b"\n", b"\r\n", b" ", b"  ", b"word", b"\xc3\xa9", b"\xff", b"\x00", b"**"]  # fmt: skip
    for _ in range(300):
        data = b"".join(rng.choice(pieces) for _ in range(rng.randint(0, 60)))
        _roundtrip(data) if _decodes(data) else extract("a.md", data)


def _decodes(data: bytes) -> bool:
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    return True
