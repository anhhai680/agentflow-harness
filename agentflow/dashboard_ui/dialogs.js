"use strict";

// ------------------------------------------------------------------ dialogs

function dialog(title, body, onOk, okText, wide) {
  const dlg = $("dlg");
  dlg.classList.toggle("wide", !!wide);
  $("dlg-title").textContent = title;
  $("dlg-body").replaceChildren(...body);
  const ok = $("dlg-ok").cloneNode(true);
  const cancel = $("dlg-cancel").cloneNode(true);
  $("dlg-ok").replaceWith(ok);
  $("dlg-cancel").replaceWith(cancel);
  ok.textContent = okText || "OK";
  const run = ++dialogRun;
  ok.addEventListener("click", async () => {
    clearDialogMessage();
    let done = false;
    let said = "";
    dialogSaid = "";
    try { done = await onOk(); } finally { said = dialogSaid; dialogSaid = null; }
    if (run !== dialogRun || !dlg.open) { if (said) toast(said); return; }  // closed meanwhile: float, close nothing
    if (done) {
      dlg.close();
      if (said) toast(said);
    } else if (said) showDialogMessage(said);
  });
  cancel.addEventListener("click", () => dlg.close());
  clearDialogMessage();
  dlg.showModal();
}

function rejectDialog(runId, target, initial = "") {
  const note = el("textarea", { id: "reject-note", placeholder: "What should change? The phase runs again with this note." });
  note.value = initial;
  dialog("Reject " + target + " (" + runId + ")", [el("label", { for: "reject-note", text: "Note" }), note], () => {
    if (!note.value.trim()) { toast("A rejection needs a note"); return false; }
    return act(runId, "reject", { target: target, note: note.value });
  });
}

function answerDialog(item) {
  const inputs = {};
  const body = [];
  for (const q of item.questions) {
    const id = "answer-" + q.id;
    const input = el("input", { type: "text", id: id, placeholder: "an option key or your answer" });
    inputs[q.id] = input;
    body.push(el("label", { for: id, text: q.id + ": " + q.question }));
    if (q.evidence && q.evidence.length) body.push(el("div", { class: "sub", text: "see " + q.evidence.join(", ") }));
    if (q.options && q.options.length) {
      body.push(el("div", { class: "opts" }, q.options.map((o) => el("button", {
        type: "button", text: o.key + ": " + o.label + (o.recommended ? " (recommended)" : ""),
        onclick: () => { input.value = o.key; },
      }))));
    }
    body.push(input);
  }
  dialog("Answer " + item.key + " (" + item.run_id + ")", body, () => {
    const answers = {};
    for (const [qid, input] of Object.entries(inputs)) if (input.value.trim()) answers[qid] = input.value;
    if (!Object.keys(answers).length) { toast("Fill in at least one answer"); return false; }
    return act(item.run_id, "answer", { key: item.key, answers: answers });
  });
}
