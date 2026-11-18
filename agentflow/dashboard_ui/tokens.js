"use strict";

// The Metrics tab's "Where the tokens go": pure helpers with no DOM, so node can test them. `kinds` is
// `totals.step_kinds` of /api/metrics: one entry per step kind (build.implement, ...).

function compact(n) {
  if (n < 1000) return String(n);
  if (n < 1e6) return (n / 1e3).toFixed(1) + "K";
  return (n / 1e6).toFixed(1) + "M";
}

const usd2 = (value) => "$" + (value || 0).toFixed(2);

function tokenRows(kinds) {
  return (kinds || []).map((k) => [
    k.kind, String(k.sessions), usd2(k.cost_usd), usd2(k.usd.cache_write), usd2(k.usd.cache_read), usd2(k.usd.output),
    k.turns_p50 + " · " + k.turns_p90, compact(k.prefix_write_p50), compact(k.tool_output_chars) + " chars",
  ]);
}
