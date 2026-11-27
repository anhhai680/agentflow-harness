// A page whose elements remember what a view file set on them: `$` hands back one plain node per id, kept in `nodes`.
var nodes = {};
var $ = (id) => nodes[id] || (nodes[id] = { id, hidden: false, disabled: false, textContent: "", kids: [],
  classList: { toggle(c, on) { this[c] = on; } }, replaceChildren(...k) { this.kids = k; }, focus() { this.focused = true; },
  addEventListener() {} });
