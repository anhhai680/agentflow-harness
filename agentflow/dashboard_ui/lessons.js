"use strict";

// The Lessons tab: every lesson with its evidence (each source opens its run) and its target before and after.
// Titles and bodies are model or hand text: they only ever go in through el()'s textContent.
const lessonsUi = { data: null, request: 0, shown: "" };

function measure(value, runs) {
  return value === null || value === undefined ? "-" : (+value.toPrecision(4)) + " (" + runs + " run" + (runs === 1 ? "" : "s") + ")";
}

function sourceNodes(sources) {
  if (!sources.length) return [el("span", { class: "muted", text: "-" })];
  return sources.map((src) => src.run_id
    ? el("button", { type: "button", class: "src", title: "open run " + src.run_id + ", event " + src.seq, text: src.ref, onclick: () => goRun(src.run_id) })
    : el("span", { class: "chip", text: src.ref }));
}

function renderLessons() {
  const data = lessonsUi.data;
  if (!data) return;
  const body = [];
  if (!data.enabled) body.push(el("p", { class: "muted", text: "Lessons are off (memory.lessons_max_tokens is 0): no step carries them." }));
  if (!data.lessons.length) {
    body.push(el("p", { class: "muted", text: "No lessons." }));
  } else {
    body.push(table(["Lesson", "Verdict", "Target", "Baseline", "After", "Cited", "Applies", "Review by", "Evidence"], data.lessons.map((l) => el("tr", {},
      el("td", { class: "wrap" }, el("div", { text: l.id + " " + l.title }), el("div", { class: "muted", text: l.body })),
      el("td", { class: l.verdict === "improved" ? "verdict-improved" : l.verdict === "worse" ? "verdict-worse" : l.verdict === "no change" ? null : "verdict-muted", text: l.verdict }),
      el("td", { text: l.target || "-", title: l.recorded_baseline ? "recorded baseline: " + l.recorded_baseline : null }),
      el("td", { class: "num", text: measure(l.baseline, l.baseline_runs) }),
      el("td", { class: "num", text: measure(l.after, l.after_runs) }),
      el("td", { class: "num", text: l.cited }),
      el("td", { text: l.applies.length ? l.applies.join(", ") : "every phase" }),
      el("td", { class: l.expired ? "verdict-worse" : null, text: (l.review_by || "-") + (l.expired ? " (past)" : "") }),
      el("td", { class: "wrap" }, sourceNodes(l.sources)))), [3, 4, 5]));
  }
  body.push(el("p", { class: "muted", text: data.note }));
  $("les-body").replaceChildren(...body);
}

async function loadLessons() {
  const request = ++lessonsUi.request;
  try {
    const data = await api("/api/lessons");
    if (request !== lessonsUi.request) return;
    $("les-status").textContent = data.lessons.length + " lesson" + (data.lessons.length === 1 ? "" : "s") + " · updated " + data.generated_at.slice(11, 19);
    lessonsUi.data = data;
    const shown = JSON.stringify(data.lessons);
    if (shown !== lessonsUi.shown) {  // a live refresh with nothing new keeps the table as it is
      lessonsUi.shown = shown;
      renderLessons();
    }
  } catch (err) {
    if (request === lessonsUi.request) $("les-status").textContent = "Could not load: " + err.message;
  }
}
