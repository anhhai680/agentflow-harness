"use strict";

// ------------------------------------------------------------------ workflow view

const STATUS_CLASS = { done: "st-ok", approved: "st-ok", failed: "st-bad", waiting: "st-wait", awaiting_approval: "st-wait", running: "st-run", refining: "st-run" };

function stepSkills(step) {
  const skills = [];
  if (step.skill) skills.push(step.skill);
  if (step.small_plan_skill) skills.push(step.small_plan_skill + " (small plan)");
  if (skills.length) return skills.join(" · ");
  if (step.type === "agent" && !step.tdd) return "no skill (no TDD for prompt text)";  // an agent step no skill owns
  return { gate: "no agent", finish: "runner action", commit: "runner action", ci: "runner action", group: "", checkpoint: "human checkpoint" }[step.type] ?? "no skill";
}

// One workflow for all work: a badge marks only a step one kind of work runs, and a nested step doesn't repeat its parent's.
function onlyFor(step, parent) {
  if (step.lanes.length !== 1 || ui.workflow.lanes.length < 2 || (parent && parent.lanes.length === 1)) return null;
  return el("div", {}, el("span", { class: "badge", text: workLabel(step.lanes[0]) + " only" }));
}

function workflowStep(step, parent) {
  const facts = [step.model, step.gates.length ? "gates: " + step.gates.join(", ") : null, step.runs_when ? "when: " + step.runs_when : null].filter(Boolean).join(" · ");
  const children = [...step.steps, ...(step.review ? [step.review] : [])];
  return el("div", { class: "wf-step" },
    el("div", { class: "title", text: step.id }),
    el("div", { class: "sub", text: stepSkills(step) }),
    onlyFor(step, parent),
    facts ? el("div", { class: "sub", text: facts }) : null,
    children.map((child) => workflowStep(child, step)));
}

function renderWorkflow() {
  const view = ui.workflow;
  if (!view) return;
  $("workflow-phases").replaceChildren(...view.phases.map((phase) => el("div", { class: "col" },
    el("h2", {}, el("span", { text: phase.name }), el("span", { text: phase.approval ? "you approve" + (phase.human_skill ? ", offers " + phase.human_skill : "") : "moves on by itself" })),
    phase.steps.map((step) => workflowStep(step)))));
  $("workflow-human").replaceChildren(...view.human_skills.map((skill) => el("span", { class: "badge", text: skill })));
  $("workflow-human-note").textContent = humanSkillsNote(view);
}

// The pack's user-only skills (`guardrails.user_only_skills`) and the approval pages that offer one.
function humanSkillsNote(view) {
  if (!view.human_skills.length) return "This pack names no human-run skills (guardrails.user_only_skills).";
  const offered = view.phases.filter((phase) => phase.human_skill)
    .map((phase) => phase.human_skill + " when the " + phase.name + " approval page offers it");
  return "The runner never loads these (guardrails.user_only_skills). You run "
    + (offered.length ? offered.join(", ") + ", and the others when you choose." : "them when you choose.");
}

function runStepNode(step) {
  const detail = [step.count, step.reason].filter(Boolean).join(" · ");
  return el("li", {},
    el("span", { class: STATUS_CLASS[step.status] || "muted", text: step.status }), " ",
    el("strong", { text: step.id }),
    step.skill ? el("span", { class: "muted", text: " " + step.skill }) : null,
    detail ? el("div", { class: "muted", text: detail }) : null,
    step.steps.length ? el("ul", { class: "wf-run" }, step.steps.map(runStepNode)) : null);
}

function runWorkflow(phases) {
  return el("div", {}, el("h3", { class: "wf-title", text: "Workflow" }), phases.map((phase) => el("div", { class: "wf-phase" },
    el("div", {}, el("strong", { text: phase.name }), " ", el("span", { class: STATUS_CLASS[phase.status] || "muted", text: phase.status.replace("_", " ") }),
      phase.reason ? el("span", { class: "muted", text: " · " + phase.reason }) : null),
    el("ul", { class: "wf-run" }, phase.steps.map(runStepNode)))));
}
