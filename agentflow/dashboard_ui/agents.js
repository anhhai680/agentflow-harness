"use strict";

// ------------------------------------------------------------------ agents view

// Groups in order of each run's first row; within a group the idle rows come first, then the longest running.
function agentsByRun(rows) {
  const groups = [];
  const by = new Map();
  for (const r of rows) {
    let g = by.get(r.run_id);
    if (!g) { g = { run_id: r.run_id, module: r.module, rows: [] }; by.set(r.run_id, g); groups.push(g); }
    g.rows.push(r);
  }
  for (const g of groups) g.rows.sort((a, b) => (b.idle ? 1 : 0) - (a.idle ? 1 : 0) || (b.age_s || 0) - (a.age_s || 0));
  return groups;
}

function agentTable(rows, withSubs) {
  const head = ["Session", "Phase › step", "Role", "Agent", "Model", "Effort", "Status", "Ctx", "Spend", "For", "Last activity"];
  const body = [];
  for (const g of agentsByRun(rows)) {
    body.push(el("tr", { class: "group" }, el("th", { colspan: String(head.length) },
      el("a", { href: routeHash({ view: "run", run: g.run_id, tab: SESSIONS_TAB }), text: g.module }))));
    for (const r of g.rows) {
      const effort = el("td", { title: r.effort_reason ? "chose " + r.effort + ": " + r.effort_reason : r.effort_refused ? "refused: " + JSON.stringify(r.effort_refused) : null }, (r.effort || "-") + arrow(r.effort_arrow));
      const last = r.idle ? "idle " + ago(r.quiet_s) + " · " + (r.last_tool || "") : (r.last_tool || "");
      body.push(el("tr", { class: "row-link" + (r.idle ? " idle" : ""), title: "open " + (r.label || r.run_id), onclick: () => (r.session ? goRun(r.run_id, SESSIONS_TAB, r.session) : goRun(r.run_id)) },
        el("td", { text: r.label || "-" }), el("td", { text: r.place }), el("td", { text: r.role || "—" }), el("td", { text: r.agent || "-" }),
        el("td", { text: r.model || "-", title: r.model_source ? "from " + r.model_source : null }), effort,
        el("td", { text: r.status || "-" }), el("td", { text: typeof r.context_pct === "number" ? Math.round(r.context_pct) + "%" : "-" }),
        el("td", { text: money(r.spend) }), el("td", { text: ago(r.age_s) }),
        el("td", { text: last })));
      if (withSubs) {
        for (const s of r.subagents) {
          body.push(el("tr", { class: "sub" }, el("td", { text: "  └ sub" }), el("td", { text: (s.subagent_type || "subagent") + " (spawned by the session above)" }),
            el("td", { text: "—" }), el("td", { text: r.agent || "-" }), el("td", { text: s.model }), el("td", { text: "inherit" }),
            el("td", { text: s.done ? "done" : "running" }), el("td", { text: "—" }), el("td", { text: "—" }), el("td", { text: "" }), el("td", { text: "" })));
        }
      }
    }
  }
  return el("table", {}, el("thead", {}, el("tr", {}, head.map((h) => el("th", { text: h })))), el("tbody", {}, body));
}

function renderAgents(view) {
  ui.agents = view;
  const byAgent = Object.entries(view.counts.by_agent).map(([k, v]) => k + " " + v).join(" · ");
  const byModel = Object.entries(view.counts.by_model).map(([k, v]) => k + " " + v).join(" · ");
  $("agents-summary").textContent = "Agents running: " + view.live.length + (view.live.length ? "  (" + byAgent + " | " + byModel + ")" : "");
  $("agents-live").replaceChildren(view.live.length ? agentTable(view.live, true) : el("p", { class: "muted", text: "No agent is running." }));
  $("exited-title").textContent = "Exited in the last " + view.show_exited_min + " min";
  $("agents-exited").replaceChildren(view.exited.length ? agentTable(view.exited, false) : el("p", { class: "muted", text: "None." }));
}
