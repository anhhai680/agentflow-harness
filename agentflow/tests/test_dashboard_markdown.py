"""The dashboard's Markdown renderer: a pure data tree first, DOM nodes second, never HTML. Run with node, no DOM."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agentflow.dashboard import UI_DIR

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not on PATH")
LOADER = ("const fs=require('fs'),vm=require('vm');const ctx=vm.createContext({});"
          "for (const f of JSON.parse(process.argv[1])) vm.runInContext(fs.readFileSync(f,'utf8'),ctx);"
          "ctx.args=JSON.parse(fs.readFileSync(0,'utf8'));"
          "process.stdout.write(JSON.stringify(vm.runInContext(process.argv[2],ctx)));")


def js(files: list[str], expr: str, args) -> object:
    out = subprocess.run([NODE, "-e", LOADER, json.dumps([str(UI_DIR / f) for f in files]), expr],
                         input=json.dumps(args), capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def walk(nodes):
    for node in nodes:
        yield node
        yield from walk(node.get("c") or [])


HOSTILE = "\n".join([
    "<script>alert(1)</script>", "", "[a](javascript:alert(1)) [b](JAVASCRIPT:x) [c]( javascript:x)",
    "[d](//evil.example/x) [e](data:text/html,x) [f](vbscript:x) [g](/etc/passwd) [h](\\\\host\\share)",
    "<img src=x onerror=alert(1)> &lt;b&gt; &#60;", "", "| <b>x</b> | y |", "|---|---|", "| 1 | 2 |",
])


def test_hostile_markdown_comes_out_as_text():
    blocks = js(["markdown.js"], "parseMarkdown(args)", HOSTILE)
    nodes = list(walk(blocks))
    assert {n["t"] for n in nodes} <= set(js(["markdown.js"], "MD_TAGS", None))
    assert not [n for n in nodes if n["t"] in ("a", "ref")]
    texts = "".join(n.get("v", "") for n in nodes)
    assert "<script>alert(1)</script>" in texts and "&lt;b&gt; &#60;" in texts and "<b>x</b>" in texts


def test_only_web_and_repo_links_survive():
    nodes = list(walk(js(["markdown.js"], "parseMarkdown(args)", "[w](https://example.com/a) [r](docs/x.md#s)")))
    assert [(n["t"], n.get("href") or n.get("path")) for n in nodes if n["t"] in ("a", "ref")] == [
        ("a", "https://example.com/a"), ("ref", "docs/x.md#s")]


def test_blocks_carry_their_source_lines():
    text = "# T\n\npara one\npara two\n\n- a\n- b\n\n```py\nx = 1\n```\n"
    blocks = js(["markdown.js"], "parseMarkdown(args)", text)
    assert [(b["t"], b["line"], b["end"]) for b in blocks] == [("h1", 1, 1), ("p", 3, 4), ("ul", 6, 7), ("pre", 9, 11)]
    assert blocks[3]["v"] == "x = 1"


@pytest.mark.parametrize("target,expected", [
    ("https://example.com/a", {"t": "a", "href": "https://example.com/a"}),
    ("HTTP://example.com", {"t": "a", "href": "HTTP://example.com"}),
    ("docs/x.md", {"t": "ref", "path": "docs/x.md"}),
    ("x.md#sec", {"t": "ref", "path": "x.md#sec"}),
    ("javascript:alert(1)", None), (" javascript:x", None), ("\tjavascript:x", None), ("java\nscript:x", None),
    ("mailto:a@b.c", None), ("data:text/html,x", None), ("//evil.example/x", None), ("/etc/passwd", None),
    ("\\\\host\\share", None), ("", None), ("\u0001javascript:x", None),
])
def test_safe_target_allow_list(target, expected):
    assert js(["markdown.js"], "safeTarget(args)", target) == expected


def test_inline_nodes_and_structure():
    text = "Some **bold** and *em* and `co*de*` and _it_.\n"
    (block,) = js(["markdown.js"], "parseMarkdown(args)", text)
    assert block["t"] == "p"
    kinds = [(n["t"], n.get("v") or [c.get("v") for c in n.get("c", [])]) for n in block["c"]]
    assert ("strong", ["bold"]) in kinds and ("em", ["em"]) in kinds and ("code", "co*de*") in kinds and ("em", ["it"]) in kinds


def test_lists_tables_quotes_and_rules():
    text = "- a\n  - nested\n- b\n\n1. one\n2. two\n\n| h1 | h2 |\n|:--|--:|\n| 1 | 2 |\n\n> quoted\n> more\n\n---\n\n~~~\nraw <b>\n~~~\n"
    blocks = js(["markdown.js"], "parseMarkdown(args)", text)
    assert [b["t"] for b in blocks] == ["ul", "ol", "table", "blockquote", "hr", "pre"]
    ul = blocks[0]
    assert [i["t"] for i in ul["c"]] == ["li", "li"]
    assert any(n["t"] == "ul" for n in walk(ul["c"][0]["c"]))
    assert blocks[2]["align"] == ["left", "right"]
    assert blocks[5]["v"] == "raw <b>"


def test_unterminated_fence_and_empty_input_do_not_throw():
    assert js(["markdown.js"], "parseMarkdown(args)", "") == []
    (block,) = js(["markdown.js"], "parseMarkdown(args)", "```\nopen\nstill open")
    assert block["t"] == "pre" and block["v"] == "open\nstill open"
    assert js(["markdown.js"], "parseMarkdown(args).length", "[unclosed](x **b `c") == 1


FAKE_DOM = """
class FakeNode {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.kids = []; this.text = ""; }
  setAttribute(k, v) { this.attrs[k] = v; }
}
function el(tag, props, ...children) {  // the shape of core.js's el(): `text` is the only way a string is set on a node
  const node = new FakeNode(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "text") node.text = String(v); else node.setAttribute(k, String(v));
  }
  for (const child of children.flat()) if (child !== null && child !== undefined) node.kids.push(typeof child === "string" ? {tag: "#text", text: child} : child);
  return node;
}
"""


def test_markdown_nodes_builds_dom_from_the_tree(tmp_path):
    fake = tmp_path / "fake_dom.js"
    fake.write_text(FAKE_DOM, encoding="utf-8")
    text = "# T <b> [w](https://example.com/a) [r](docs/x.md)\n\n```\n<i>raw</i>\n```\n\n| a |\n|:-:|\n| 1 |\n"
    out = js([str(fake), "markdown.js"], "markdownNodes(parseMarkdown(args))", text)
    h1, pre, table = out
    assert h1["tag"] == "h1" and h1["attrs"] == {"data-line": "1", "data-end": "1"}
    link = next(k for k in h1["kids"] if k["tag"] == "a")
    assert link["attrs"] == {"href": "https://example.com/a", "target": "_blank", "rel": "noopener noreferrer"}
    ref = next(k for k in h1["kids"] if k["tag"] == "span")
    assert [k["tag"] for k in ref["kids"]] == ["#text", "#text", "code"] and ref["kids"][2]["text"] == "docs/x.md"
    assert "<b>" in "".join(k.get("text", "") for k in h1["kids"])  # raw HTML stays text
    assert pre["tag"] == "pre" and pre["kids"][0]["text"] == "<i>raw</i>"
    cell = table["kids"][0]["kids"][0]["kids"][0]
    assert cell["tag"] == "th" and cell["attrs"]["class"] == "al-center"


# --- the output for ordinary Markdown does not change -------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "markdown"


def test_ordinary_markdown_parses_to_the_pinned_tree():
    text = (FIXTURES / "sample.md").read_text(encoding="utf-8")
    assert js(["markdown.js"], "parseMarkdown(args)", text) == json.loads((FIXTURES / "sample.json").read_text(encoding="utf-8"))


# --- no input freezes the page or overflows the stack ---------------------------------------------------------------

SIZE = 512 * 1024  # the default artifact cap
BOUND_S = 3.0      # generous: the shapes below take a fraction of a second, and were minutes before the fix
REALISTIC = ("# Heading #\n\nSome **bold** text with `code` and a [link](docs/x.md) and _em_ words here.\n\n- item one\n"
             "- item **two**\n  - nested `x`\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```py\nx = 1\n```\n\n> quote line\n\n")


def repeated(unit: str, size: int = SIZE, sep: str = "") -> str:
    return sep.join([unit] * (size // (len(unit) + len(sep)) + 1))


# Each case parses, then builds the DOM, and reports the time taken inside node (so the start-up of node doesn't count).
TIMED = ("(function(){const t=Date.now();const nodes=markdownNodes(parseMarkdown(args));"
         "return {ms:Date.now()-t,count:nodes.length};})()")

SLOW_SHAPES = {
    "realistic": REALISTIC * (SIZE // len(REALISTIC) + 1),
    "underscore": repeated("_a "), "underscore_lines": repeated("_a ", sep="\n"), "star": repeated("*a "),
    "double_star": repeated("**a "), "bracket": repeated("[a "), "bracket_paren": repeated("[a]("),
    "backtick": repeated("`a "), "nested_marks": "*_" * (SIZE // 4) + "x" + "_*" * (SIZE // 4),
    "heading_regex": "# a" + " " * 80_000 + "x", "heading_tabs": "# a" + " \t" * 40_000 + "x",
    "delim_regex": "a|b\n" + " " * 80_000 + "x|", "rule_regex": "-" + " -" * 40_000 + " x", "list_regex": " " * 80_000 + "x",
    "table": "| a | b |\n|---|---|\n" + repeated("| 1 | 2 |", sep="\n"), "list_items": repeated("- item", sep="\n"),
    "quote_lines": repeated("> x", sep="\n"),
}


@pytest.mark.parametrize("shape", sorted(SLOW_SHAPES))
def test_parsing_a_large_or_hostile_input_stays_fast(shape, tmp_path):
    fake = tmp_path / "fake_dom.js"
    fake.write_text(FAKE_DOM, encoding="utf-8")
    out = js([str(fake), "markdown.js"], TIMED, SLOW_SHAPES[shape])
    assert out["count"] >= 1
    assert out["ms"] < BOUND_S * 1000, f"{shape} took {out['ms']} ms"


COLLECT = ("(function(){const texts=[];const walk=(n)=>{if(n===null||n===undefined)return;if(typeof n==='string'){texts.push(n);return;}"
           "if(n.t==='text'||n.t==='code'||n.t==='pre'){texts.push(n.v);}for(const c of (n.c||[]))walk(c);};"
           "const blocks=parseMarkdown(args);for(const b of blocks)walk(b);"
           "const dom=[];const dwalk=(n)=>{if(n.text)dom.push(n.text);for(const k of (n.kids||[]))dwalk(k);};"
           "for(const n of markdownNodes(blocks))dwalk(n);"
           "return {tree:texts.join(''),dom:dom.join(''),blocks:blocks.length};})()")

DEEP_SHAPES = {
    "quote_markers": ">" * 100_000 + " x",
    "quote_markers_spaced": "> " * 50_000 + "x",
    "quote_lines": "\n".join(">" * (d + 1) + " x" for d in range(1500)),
    "list_indents": "\n".join(" " * d + "- x" for d in range(2000)),
    "list_indents_by_two": "\n".join(" " * (2 * d) + "- x" for d in range(1000)),
    "nested_links": "[" * 100_000 + "x" + "](a)" * 100_000,
    "nested_brackets": "[" * 100_000 + "x" + "]" * 100_000,
    "nested_emphasis": "*_" * 50_000 + "x" + "_*" * 50_000,
    "nested_strong": "**a " * 50_000 + "x" + " a**" * 50_000,
    "mixed": "> - " * 5_000 + "[**_x_**](a)",
}


@pytest.mark.parametrize("shape", sorted(DEEP_SHAPES))
def test_deep_nesting_never_throws_and_keeps_the_text(shape, tmp_path):
    fake = tmp_path / "fake_dom.js"
    fake.write_text(FAKE_DOM, encoding="utf-8")
    out = js([str(fake), "markdown.js"], COLLECT, DEEP_SHAPES[shape])  # check=True: a RangeError fails the test
    assert out["blocks"] >= 1
    assert "x" in out["tree"] and "x" in out["dom"]  # (a link ref adds its path to the DOM, so the two differ there)


def test_nesting_is_capped_at_32_levels():
    depth = ("(function(){let d=0;const walk=(n,k)=>{d=Math.max(d,k);for(const c of (n.c||[]))walk(c,k+1);};"
             "for(const b of parseMarkdown(args))walk(b,1);return d;})()")
    assert js(["markdown.js"], depth, ">" * 5000 + " x") <= 40
    assert js(["markdown.js"], depth, "[" * 5000 + "x" + "](a)" * 5000) <= 40
    assert js(["markdown.js"], depth, "\n".join(" " * d + "- x" for d in range(300))) <= 100


def test_a_finding_on_line_34_marks_line_34_raw_and_its_block_rendered():
    text = "\n".join(["# T", ""] + ["" if n in (32, 36) else f"line {n}" for n in range(3, 41)])  # 33–35 is one paragraph
    rows = [{"open": True, "findings": [{"severity": "high", "file": "s.md", "line": 34, "text": "x"},
                                        {"severity": "low", "file": "s.md", "line": 34, "text": "y"},
                                        {"severity": "high", "file": "s.md", "line": 999, "text": "past the end"},
                                        {"severity": "high", "file": "other.md", "line": 3, "text": "z"}]},
            {"open": False, "findings": [{"severity": "critical", "file": "s.md", "line": 3, "text": "old"}]}]
    assert js(["artifact.js"], "rawMarks(args.rows, 's.md')", {"rows": rows}) == {"34": "high", "999": "high"}
    marks = js(["markdown.js", "artifact.js"], "gutterMarks(parseMarkdown(args.text), args.rows, 's.md')",
               {"text": text, "rows": rows})
    block = next(b for b in js(["markdown.js"], "parseMarkdown(args)", text) if b["line"] <= 34 <= b["end"])
    assert marks == {str(block["line"]): "high"}


def test_the_default_artifact_is_the_markdown_file_with_most_revisions():
    files = [{"path": "a.txt", "kind": "spec", "revisions": [1, 2, 3]}, {"path": "b.md", "kind": "spec", "revisions": [1]},
             {"path": "c.md", "kind": "spec", "revisions": [1, 2]}, {"path": "p.md", "kind": "plan", "revisions": [1]}]
    assert js(["artifact.js"], "pickArtifact(args, 'spec', null).path", files) == "c.md"
    assert js(["artifact.js"], "pickArtifact(args, 'spec', 'b.md').path", files) == "b.md"
    assert js(["artifact.js"], "pickArtifact(args, 'spec', 'p.md').path", files) == "c.md"
    assert js(["artifact.js"], "pickArtifact(args, 'plan', null) && 1", []) is None


def test_a_failed_read_keeps_the_last_document_or_says_why_and_never_waits_forever():
    choice = lambda *a: js(["artifact.js"], "docChoice(...args)", list(a))  # noqa: E731
    assert choice(True, None, False) == "ready" and choice(True, "boom", True) == "ready"
    assert choice(False, None, True) == "loading" and choice(False, None, False) == "loading"
    assert choice(False, "HTTP 500", True) == "stale"  # the last text stays; the toast says why
    assert choice(False, "HTTP 500", False) == "error"  # nothing drawn yet: an error note, not "Loading…"
    assert choice(False, "", False) == "error"  # an empty reason is still a failure


def test_comments_compose_into_one_line_anchored_note():
    note = js(["artifact.js"], "composeNote('docs/specs/x.md', 'abcdef0123', args)",
              [[65, "repeat loop\n takes  locks"], [34, "Scope 3 contradicts the code"], [50, "  "]])
    assert note == ("Comments on docs/specs/x.md at abcdef0:\n"
                    "L34: Scope 3 contradicts the code\nL65: repeat loop takes locks")


def test_coming_back_to_an_artifact_tab_refetches_and_moving_within_it_does_not():
    reopens = "reopensArtifact(args.prev, 'r1', args.tab, args.st)"
    st = {"runId": "r1", "list": []}
    case = lambda prev, tab, state=st: js(["artifact.js"], reopens, {"prev": prev, "tab": tab, "st": state})  # noqa: E731
    assert case({"id": "r1", "tab": "overview"}, "spec") is True  # Spec -> Overview -> Spec
    assert case(None, "spec") is True  # Spec -> Board -> the same run's Spec
    assert case({"id": "r2", "tab": "spec"}, "spec") is True  # from another run
    assert case({"id": "r1", "tab": "spec"}, "spec") is False  # another file or a scroll target on the same tab
    assert case({"id": "r1", "tab": "overview"}, "gates") is False  # not an artifact tab
    assert case(None, "spec", {"runId": "r2", "list": []}) is False  # no state of this run: the tab loads it anyway
    assert case(None, "spec", None) is False
    assert case(None, "spec", {"runId": "r1", "list": None, "error": "HTTP 500"}) is True  # a failed first load is tried again


def test_the_viewed_list_drops_an_unresolved_head_and_keeps_the_newest_up_to_the_cap():
    expr = ("this.ui = {viewed: new Map()};"
            "recordViewed('r', 'p.md', 'head');"
            "for (let i = 0; i < 150; i++) recordViewed('r', 'p.md', i.toString(16).padStart(7, '0'));"
            "recordViewed('r', 'p.md', 'head');"
            "viewedList('r')")
    got = js(["artifact.js"], expr, None)
    assert len(got) == 100 and all(v["sha"] != "head" for v in got)
    assert got[0] == {"path": "p.md", "sha": format(50, "07x")} and got[-1] == {"path": "p.md", "sha": format(149, "07x")}
    assert js(["artifact.js"], "this.ui = {viewed: new Map()}; viewedList('none')", None) == []


def test_the_size_note_is_the_drawn_documents_and_rounds_up():
    note = lambda mode, text, diff: js(["artifact.js"], "sizeNote(...args)", [mode, text, diff])  # noqa: E731
    text = {"text": "x" * 16383, "truncated": True}
    diff = {"diff": "+y\n" * 10, "truncated": False}
    assert note("rendered", text, diff) == {"truncated": True, "kb": 16}  # 16383 bytes reads 16 KB, not 15
    assert note("raw", text, diff) == {"truncated": True, "kb": 16}
    assert note("diff", text, diff) == {"truncated": False, "kb": 0}  # the diff drawn is whole
    assert note("diff", None, {"diff": "é" * 8192 + "+", "truncated": True}) == {"truncated": True, "kb": 17}  # UTF-8 bytes
    assert note("rendered", {"text": "x", "truncated": False}, {"diff": "", "truncated": True}) == {"truncated": False, "kb": 0}
    assert note("diff", text, None) == {"truncated": False, "kb": 0}  # the diff is still being read


def test_request_changes_in_diff_mode_carries_the_selected_revisions_comments():
    full = "a" * 40
    expr = ("this.ui = {comments: new Map()};"
            "commentsFor('r', 'p.md', args.full).set(3, 'tighten this');"
            "commentsFor('r', 'p.md', 'b'.repeat(40)).set(5, 'on another revision');"
            "const data = {mode: 'diff', path: 'p.md', rsha: shownSha('head', {to: args.full, diff: ''})};"
            "noteOfShown({runId: 'r', shown: {key: 'k', data: data}})")
    got = js(["artifact.js"], expr, {"full": full})
    assert got == {"count": 1, "note": "Comments on p.md at aaaaaaa:\nL3: tighten this"}
    assert js(["artifact.js"], "shownSha('head', {sha: args})", full) == full
    assert js(["artifact.js"], "shownSha(args, undefined)", full) == full


def test_the_findings_are_the_artifacts_phases_or_on_its_files():
    rows = [{"key": "define/spec_review", "findings": [{"file": "docs/specs/x.md"}]},
            {"key": "plan/plan_review", "findings": [{"file": "docs/plans/x.md"}]},
            {"key": "build/task_1/review", "findings": [{"file": "src/a.py"}]},
            {"key": "build/task_2/review", "findings": [{"file": "src/b.py"}, {"file": "docs/plans/x.md", "line": 3}]},
            {"key": "plan/plan_review", "findings": []}]
    keys = js(["artifact.js"], "findingRows(args, 'plan', ['docs/plans/x.md']).map((r) => r.key)", rows)
    assert keys == ["plan/plan_review", "build/task_2/review", "plan/plan_review"]
    assert js(["artifact.js"], "findingRows(args, 'define', ['docs/specs/x.md']).map((r) => r.key)", rows) == [
        "define/spec_review"]
    assert js(["artifact.js"], "findingRows(null, 'define', []).length", None) == 0
