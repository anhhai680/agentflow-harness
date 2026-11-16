// A safe Markdown renderer. Text becomes a plain data tree first (parseMarkdown, pure, no DOM), and DOM nodes
// second (markdownNodes). No raw HTML ever: anything unrecognised, raw HTML included, is text, entities are not
// decoded, and every string reaches the DOM as a text node. The top level declares only functions and literals.

const MD_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td",
  "pre", "code", "blockquote", "hr", "strong", "em", "a", "ref", "text"];
const MD_FENCE = /^ {0,3}(`{3,}|~{3,})/;
const MD_HEADING = /^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$/;
const MD_RULE = /^ {0,3}([-*_])(?: *\1){2,} *$/;
const MD_ITEM = /^( *)([-*+]|\d{1,9}[.)])[ \t]+(.*)$/;
const MD_QUOTE = /^ {0,3}>[ ]?/;
const MD_DELIM = /^ *\|? *:?-+:? *(?:\| *:?-+:? *)*\|? *$/;
const MD_WEB = /^https?:\/\//i;
const MD_SCHEME = /^[A-Za-z][A-Za-z0-9+.-]*:/;
const MD_UNSAFE_CHAR = /[\s\u0000-\u001f\u007f-\u009f]/;
const MD_PUNCT = /[!-\/:-@\[-`{-~]/;
const MD_WORD = /[A-Za-z0-9]/;

// The only places a link may lead: a web page, or a path in the repository (shown, never followed, in U1).
function safeTarget(target) {
  const t = String(target);
  if (!t || MD_UNSAFE_CHAR.test(t)) return null;
  if (MD_WEB.test(t)) return { t: "a", href: t };
  if (MD_SCHEME.test(t) || t[0] === "/" || t[0] === "\\") return null;
  return { t: "ref", path: t };
}

function mdText(nodes, v) {
  const last = nodes[nodes.length - 1];
  if (last && last.t === "text") last.v += v;
  else nodes.push({ t: "text", v });
}

// The index of the bracket that closes the one at `from`, or -1.
function mdClose(s, from, open, close) {
  let depth = 0;
  for (let i = from; i < s.length; i++) {
    if (s[i] === "\\") { i++; continue; }
    if (s[i] === open) depth++;
    else if (s[i] === close && --depth === 0) return i;
  }
  return -1;
}

function parseInline(s) {
  const nodes = [];
  let i = 0;
  while (i < s.length) {
    const ch = s[i];
    if (ch === "\\" && i + 1 < s.length && MD_PUNCT.test(s[i + 1])) { mdText(nodes, s[i + 1]); i += 2; continue; }
    if (ch === "`") {
      let n = 1;
      while (s[i + n] === "`") n++;
      const end = s.indexOf("`".repeat(n), i + n);
      if (end > i + n) { nodes.push({ t: "code", v: s.slice(i + n, end) }); i = end + n; continue; }
      mdText(nodes, "`".repeat(n)); i += n; continue;
    }
    if (ch === "[") {
      const close = mdClose(s, i, "[", "]");
      const stop = close > 0 && s[close + 1] === "(" ? mdClose(s, close + 1, "(", ")") : -1;
      if (stop > 0) {
        const kids = parseInline(s.slice(i + 1, close));
        const link = safeTarget(s.slice(close + 2, stop).trim());
        if (link) nodes.push({ ...link, c: kids });
        else for (const kid of kids) nodes.push(kid);  // an unsafe target leaves only the link's text
        i = stop + 1; continue;
      }
    }
    if (ch === "*" || ch === "_") {
      const dbl = s[i + 1] === ch;
      const mark = dbl ? ch + ch : ch;
      const from = i + mark.length;
      let end = -1;
      for (let j = from; j < s.length; j++) {
        if (s[j] === "\\") { j++; continue; }
        if (s.startsWith(mark, j) && (dbl || s[j + 1] !== ch) && j > from && !/\s/.test(s[j - 1])) { end = j; break; }
      }
      const edge = ch === "_" && (MD_WORD.test(s[i - 1] || "") || MD_WORD.test(s[end + mark.length] || ""));
      if (end > 0 && !/\s/.test(s[from] || " ") && !edge) {
        nodes.push({ t: dbl ? "strong" : "em", c: parseInline(s.slice(from, end)) });
        i = end + mark.length; continue;
      }
    }
    mdText(nodes, ch); i++;
  }
  return nodes;
}

function mdCells(row) {
  const cells = row.trim().replace(/^\|/, "").replace(/\|$/, "").split(/(?<!\\)\|/);
  return cells.map((cell) => cell.trim());
}

function mdStartsBlock(lines, i) {
  const line = lines[i];
  return MD_FENCE.test(line) || MD_HEADING.test(line) || MD_RULE.test(line) || MD_QUOTE.test(line) || MD_ITEM.test(line)
    || (line.includes("|") && i + 1 < lines.length && lines[i + 1].includes("|") && MD_DELIM.test(lines[i + 1]));
}

function mdList(lines, i, first, base, offset) {
  const ordered = /\d/.test(first[2][0]);
  const items = [];
  let k = i;
  while (k < lines.length) {
    const m = MD_ITEM.exec(lines[k]);
    if (!m || m[1].length !== base || /\d/.test(m[2][0]) !== ordered) break;
    const sub = [];
    let end = k + 1;
    while (end < lines.length && lines[end].trim() && (/^ */.exec(lines[end])[0].length > base)) sub.push(lines[end++]);
    const pad = sub.length ? /^ */.exec(sub[0])[0].length : 0;
    const kids = parseInline(m[3]);
    for (const block of mdBlocks(sub.map((l) => l.slice(Math.min(pad, /^ */.exec(l)[0].length))), offset + k + 1)) kids.push(block);
    items.push({ t: "li", line: offset + k + 1, end: offset + end, c: kids });
    k = end;
  }
  return { block: { t: ordered ? "ol" : "ul", line: offset + i + 1, end: offset + k, c: items }, next: k };
}

function mdBlocks(lines, offset) {
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const at = offset + i;  // the source line of lines[i], minus one
    let m;
    if (!line.trim()) { i++; continue; }
    if ((m = MD_FENCE.exec(line))) {
      let end = i + 1;
      while (end < lines.length && !(lines[end].trim().startsWith(m[1][0].repeat(m[1].length)) && /^[`~]+$/.test(lines[end].trim()))) end++;
      blocks.push({ t: "pre", line: at + 1, end: offset + Math.min(end, lines.length - 1) + 1, v: lines.slice(i + 1, end).join("\n") });
      i = end + 1; continue;
    }
    if ((m = MD_HEADING.exec(line))) { blocks.push({ t: "h" + m[1].length, line: at + 1, end: at + 1, c: parseInline(m[2]) }); i++; continue; }
    if (MD_RULE.test(line)) { blocks.push({ t: "hr", line: at + 1, end: at + 1 }); i++; continue; }
    if (MD_QUOTE.test(line)) {
      let end = i;
      while (end < lines.length && MD_QUOTE.test(lines[end])) end++;
      blocks.push({ t: "blockquote", line: at + 1, end: offset + end, c: mdBlocks(lines.slice(i, end).map((l) => l.replace(MD_QUOTE, "")), at) });
      i = end; continue;
    }
    if (line.includes("|") && i + 1 < lines.length && lines[i + 1].includes("|") && MD_DELIM.test(lines[i + 1])) {
      const align = mdCells(lines[i + 1]).map((c) => (c.startsWith(":") ? (c.endsWith(":") ? "center" : "left") : c.endsWith(":") ? "right" : ""));
      const row = (cells, t) => ({ t: "tr", c: cells.map((v, n) => ({ t, align: align[n] || "", c: parseInline(v) })) });
      const body = [];
      let end = i + 2;
      while (end < lines.length && lines[end].trim() && lines[end].includes("|")) body.push(row(mdCells(lines[end++]), "td"));
      blocks.push({ t: "table", line: at + 1, end: offset + end, align, c: [{ t: "thead", c: [row(mdCells(line), "th")] }, { t: "tbody", c: body }] });
      i = end; continue;
    }
    if ((m = MD_ITEM.exec(line))) {
      const { block, next } = mdList(lines, i, m, m[1].length, offset);
      blocks.push(block); i = next; continue;
    }
    let end = i + 1;
    while (end < lines.length && lines[end].trim() && !mdStartsBlock(lines, end)) end++;
    blocks.push({ t: "p", line: at + 1, end: offset + end, c: parseInline(lines.slice(i, end).map((l) => l.trim()).join("\n")) });
    i = end;
  }
  return blocks;
}

function parseMarkdown(text) { return mdBlocks(String(text).split(/\r\n|\r|\n/), 0); }

function markdownNode(n) {
  const pos = n.line ? { "data-line": n.line, "data-end": n.end } : {};
  const kids = (n.c || []).map(markdownNode);
  if (!MD_TAGS.includes(n.t)) return null;
  if (n.t === "text") return n.v;
  if (n.t === "code") return el("code", { text: n.v });
  if (n.t === "pre") return el("pre", pos, el("code", { text: n.v }));
  if (n.t === "hr") return el("hr", pos);
  if (n.t === "a") return el("a", { href: n.href, target: "_blank", rel: "noopener noreferrer" }, kids);
  if (n.t === "ref") return el("span", { class: "md-ref" }, kids, " ", el("code", { text: n.path }));
  if (n.t === "th" || n.t === "td") return el(n.t, { class: n.align ? "al-" + n.align : null }, kids);
  return el(n.t, pos, kids);
}

function markdownNodes(blocks) { return blocks.map(markdownNode).filter((node) => node !== null); }
