"""The dashboard's Markdown renderer: a pure data tree first, DOM nodes second, never HTML. Run with node, no DOM."""
from __future__ import annotations

import json
import shutil
import subprocess

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
