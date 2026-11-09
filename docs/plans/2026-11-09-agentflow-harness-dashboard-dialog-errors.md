# Dashboard dialog errors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the dashboard refuses a New task (a 400 or 409 from `POST /api/backlog`), the developer sees why: inside the open dialog, and in the dashboard's terminal. And when the refused id is a module that's already done, the reason says how to add work to it, not "start module:<id> instead".

**Architecture:** The reason is already in the response (`{"error": "..."}`), and the page already passes it to `toast()`. But `dialog()` opens `#dlg` with `showModal()`, which puts it in the browser's top layer, and `#toast` is an ordinary element (`z-index: 4`). No z-index lifts an element above the top layer, so the toast renders under the dialog and its backdrop and is gone after 2.6 s. The developer sees only the browser's console line, `Failed to load resource: … 400 (Bad Request)`. The fix: `toast()` writes into a message line inside the dialog while the dialog is open, and the dialog floats any message from a successful submit once it has closed. The server also adds the reason to the `-> 400` line it already logs.

**Tech Stack:** `agentflow/dashboard.html` (vanilla JS, no build), `agentflow/dashboard.py` (stdlib `http.server`), pytest. The page tests are assertions on the page's source text (there is no JS engine in the suite), following `test_dashboard.py`'s existing pattern.

**Spec:** `docs/agentflow-harness-proposal.md` §1d "Team backlog" (the New task form), plus the route contract in the `agentflow/dashboard.py` module docstring (`POST /api/backlog`: a bad field is a 400, a duplicate a 409, the body is `{"error": ...}`). This plan changes neither: the server's answers stay as they are, and only how the page and the log show them changes.

**Diagnosis record (2026-11-09):** I POSTed every body the form can send (`bug` with `task` or `issue`; `feature` with `new_module` with and without deps, `after` and `to_phase`) to an in-process `make_server`. I did this against this repo's pack and against a second repository's specs pack with a copy of its real state, and each one got a 200. A 400 comes only from input that `backlog.add` rejects (for example a module id that isn't a lowercase slug, an id already in the spec files, an empty "What it does", or an issue that isn't a number). So the bug is that the reason never reaches the developer, not that valid input is refused.

The reported input was a Feature card whose module id named an existing module, in order to finish a deferred task of that module's plan. Replayed through `backlog.add`, it raises `Invalid: <id> is already a module in the spec files: start module:<id> instead`, which is the 400. That advice is wrong here. The module is in the Backlog's Done list (`plan complete on main`), so it has no Start button. It counts as complete because the deferred task in its plan is a note with no `- [ ]` steps, and `agentflow explain feature --module <id>` reports every task checked and skips `build`. Task 3 makes the reason say that the module is done, and how to add work to it.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`; no third-party import, no JS library.
- No project strings in the engine (`test_portability.py` `DENYLIST`); the new copy is generic.
- The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical.
- No config key is added or changed, so no Settings field, validate check or §14 line is needed.
- A user-visible change adds one line under `## [Unreleased]` in `CHANGELOG.md` (`### Fixed`).
- Interactive session: commit only when the developer asks. A commit message is one imperative sentence ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- **A success message raised inside a submit still shows.** `newTaskDialog` calls `toast("Added to Backlog: …")`, and `act()` calls `toast(actionToast(…))`, while the dialog is still open, just before `onOk` returns true. Expected: the message floats as the normal toast after the dialog closes, rather than being lost with the closed dialog. Pinned in Task 1, step 1 (the `ok` handler re-floats the message).
- **A stale message never shows.** An error from one failed submit, or from an earlier dialog (Reject, then New task), must be cleared when a dialog opens and again when OK is pressed. Pinned in Task 1, step 1.
- **The reason is shown as text.** Server reasons echo user input (`runs after '<x>': no such item`, `module id '<x>' …`). Expected: shown literally, never parsed as HTML. Pinned in Task 1, step 1 (`textContent`, never `innerHTML`, in `toast`).
- **A long reason wraps inside the dialog.** A 60-character module id inside a reason must not widen the dialog or scroll it sideways. Pinned in Task 1, step 1 (the `#dlg-msg` rule carries `overflow-wrap: anywhere`).
- **A done module's id gets advice that fits how it became done.** Expected: `plan complete on <branch>` says to add unticked steps to its plan, `spec says done` and `done by hand` name `agentflow backlog undone <id>`, and `merged` offers only a new module id, because a veto can't reopen a merged module (`finished.recorded`). Pinned in Task 3, step 1.
- **One request's reason is never logged against the next.** `BaseHTTPRequestHandler` serves every request of a keep-alive connection on the same handler instance. Expected: a 400's reason is logged once, on its own line, and a following 403 on the same connection logs no reason. Pinned in Task 2, step 1.

---

### Task 1: Show a dialog's messages inside the dialog

**Files:**
- Modify: `agentflow/dashboard.html`: the CSS block near line 80 (`dialog …` rules), the `#dlg` markup at lines 239-246, `toast()` at lines 288-293, and `dialog()` at lines 619-632
- Modify: `CHANGELOG.md` (`## [Unreleased]` → `### Fixed`)
- Test: `agentflow/tests/test_dashboard.py` (next to `test_the_page_wires_the_backlog_actions`, around line 1014)

**Interfaces:**
- Consumes: nothing new. `toast(text)` and `dialog(title, body, onOk, okText, wide)` keep their signatures, so the 17 `toast(` callers and the 4 `dialog(` callers (New task, Reject, Answer, Save config.yaml) change nothing.
- Produces: the element `<p id="dlg-msg">` inside `#dlg`, and this rule: while `$("dlg").open`, `toast(text)` writes to `#dlg-msg` instead of `#toast`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_message_raised_while_the_dialog_is_open_shows_inside_it():
    """`showModal()` puts the dialog in the top layer, above any z-index: a #toast there is hidden under it."""
    source = (AGENTFLOW / "dashboard.html").read_text(encoding="utf-8")
    markup = source[source.index('<dialog id="dlg">'):source.index("</dialog>")]
    assert '<p id="dlg-msg" class="gate-bad" role="alert" hidden></p>' in markup
    assert markup.index('id="dlg-msg"') < markup.index('class="dlg-buttons"')  # under the fields, above OK
    css = source[:source.index("</style>")]
    assert re.search(r"#dlg-msg\s*\{[^}]*overflow-wrap:\s*anywhere", css)
    block = source[source.index("function toast(text)"):source.index("async function api(")]
    assert 'if ($("dlg").open)' in block and '$("dlg-msg")' in block
    assert "textContent = text" in block and "innerHTML" not in block


def test_the_dialog_clears_its_message_and_floats_a_success_message_once_closed():
    source = (AGENTFLOW / "dashboard.html").read_text(encoding="utf-8")
    block = source[source.index("function dialog(title"):source.index("function rejectDialog")]
    assert block.count("clearDialogMessage()") >= 2  # on open, and again before each OK
    assert block.index("clearDialogMessage()") < block.index("dlg.showModal()")
    ok = block[block.index('ok.addEventListener("click"'):block.index('cancel.addEventListener')]
    assert "clearDialogMessage()" in ok
    assert re.search(r"dlg\.close\(\);\s*if \(said\) toast\(said\)", ok)  # a success message floats after close
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest "agentflow/tests/test_dashboard.py::test_a_message_raised_while_the_dialog_is_open_shows_inside_it" "agentflow/tests/test_dashboard.py::test_the_dialog_clears_its_message_and_floats_a_success_message_once_closed" --import-mode=prepend -q`
Expected: 2 failed (`ValueError: substring not found` / assertion on `dlg-msg`).

- [ ] **Step 3: Implement it in `agentflow/dashboard.html`**

- Markup: insert `<p id="dlg-msg" class="gate-bad" role="alert" hidden></p>` between `#dlg-body` and `.dlg-buttons`.
- CSS, next to the `dialog label` rule: `#dlg-msg { margin: 10px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; }`.
- A helper, placed just above `toast`: `function clearDialogMessage() { const msg = $("dlg-msg"); msg.textContent = ""; msg.hidden = true; }`.
- `toast(text)`: when `$("dlg").open`, set `$("dlg-msg").textContent = text`, unhide it, and return. Otherwise keep today's floating behaviour unchanged.
- `dialog()`: call `clearDialogMessage()` before `dlg.showModal()`. The `ok` click handler becomes the sequence below, so that a failure's message stays in the dialog and a success's message floats after the dialog closes:

```js
  ok.addEventListener("click", async () => {
    clearDialogMessage();
    if (await onOk()) {
      const said = $("dlg-msg").textContent;
      dlg.close();
      if (said) toast(said);
    }
  });
```

- [ ] **Step 4: Add the changelog line** under `## [Unreleased]`:

```markdown
### Fixed

- The dashboard's dialogs show why a request was refused: a New task the server rejects (a 400 or 409) now names the bad field inside the open dialog, where before the message was hidden under the modal and only the browser console showed `400 (Bad Request)`.
```

- [ ] **Step 5: Run the dashboard and portability tests**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py agentflow/tests/test_portability.py --import-mode=prepend -q`
Expected: all pass, including the two new tests and the existing `test_the_page_wires_the_backlog_actions`.

- [ ] **Step 6: Commit**

```bash
git add agentflow/dashboard.html agentflow/tests/test_dashboard.py CHANGELOG.md
git commit -m "Show a dashboard dialog's error inside the dialog, where the modal no longer hides it.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2: Log a refused request's reason with its status

**Files:**
- Modify: `agentflow/dashboard.py`: `log_request` (lines 304-306), `_json` (lines 333-334), and a new constant beside `MAX_BODY` (line 72)
- Modify: `CHANGELOG.md` (the same `### Fixed` list as Task 1)
- Test: `agentflow/tests/test_dashboard.py` (next to `test_a_bad_card_is_a_400_and_a_duplicate_a_409`, around line 822)

**Interfaces:**
- Consumes: the existing `bdash` fixture and `TOKEN` in `test_dashboard.py`, plus `http.client` (already imported there).
- Produces: `LOG_REASON_MAX = 200` in `agentflow/dashboard.py`. A 4xx/5xx sent through `_json` with an `error` string logs `dashboard: <METHOD> <path> -> <code>: <reason>`, with the reason collapsed to one line and cut to `LOG_REASON_MAX` characters (with a trailing `…` when cut). Any other error line keeps today's form, `dashboard: <METHOD> <path> -> <code>`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_refused_request_logs_its_reason_with_its_status(bdash, capsys):
    assert bdash.post("/api/backlog", {"lane": "bug", "fields": {"issue": "x; rm"}})[0] == 400
    err = capsys.readouterr().err
    assert "dashboard: POST /api/backlog -> 400: --issue takes an issue number such as 42" in err
    assert TOKEN not in err


def test_a_reason_is_logged_once_and_never_against_the_next_request_on_the_connection(bdash, capsys):
    conn = http.client.HTTPConnection("127.0.0.1", bdash.port, timeout=5)  # one connection, two requests
    bad = json.dumps({"lane": "bug", "fields": {"issue": "x; rm"}})
    try:
        statuses = []
        for headers in ({"X-Agentflow-Token": TOKEN}, {}):  # the second has no token: a 403 with no reason
            conn.request("POST", "/api/backlog", body=bad, headers={"Content-Type": "application/json", **headers})
            response = conn.getresponse()
            response.read()
            statuses.append(response.status)
        assert statuses == [400, 403]
    finally:
        conn.close()
    lines = [l for l in capsys.readouterr().err.splitlines() if "/api/backlog ->" in l]
    assert lines[0].startswith("dashboard: POST /api/backlog -> 400: --issue takes")
    assert lines[1] == "dashboard: POST /api/backlog -> 403"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest "agentflow/tests/test_dashboard.py::test_a_refused_request_logs_its_reason_with_its_status" "agentflow/tests/test_dashboard.py::test_a_reason_is_logged_once_and_never_against_the_next_request_on_the_connection" --import-mode=prepend -q`
Expected: 2 failed (the 400 line has no `: --issue takes …`).

- [ ] **Step 3: Implement it in `agentflow/dashboard.py`**

- `_json(status, data)`: when `status >= 400` and `data` is a dict whose `"error"` is a `str`, keep it on the handler as `self._log_reason` before calling `_send`. `send_response` calls `log_request`, so the reason must be set before `_send` runs. `_error` goes through `_json`, so the `_config_post` 400 that sends `{"error": ...}` alongside the proposal is covered too.
- `log_request`: first take `reason = self.__dict__.pop("_log_reason", None)`, unconditionally, so that a keep-alive connection never carries it over to the next request. For `code >= 400`, append `": " + " ".join(reason.split())`, cut to `LOG_REASON_MAX`, when `reason` is set. `log_message` already replaces the token with `<token>`, so the reason passes through that redaction.
- Docstring: in the module docstring's security notes, add that a refused request's log line carries its `error` text, and that the token never appears in it.

- [ ] **Step 4: Add the changelog line** to the same `### Fixed` list:

```markdown
- The dashboard's terminal log names the reason for every refused request (`POST /api/backlog -> 400: <reason>`), not only its status.
```

- [ ] **Step 5: Run the dashboard tests**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py --import-mode=prepend -q`
Expected: all pass, including `test_a_failing_refresh_round_does_not_stop_the_loop`, which also reads `capsys.err`.

- [ ] **Step 6: Commit**

```bash
git add agentflow/dashboard.py agentflow/tests/test_dashboard.py CHANGELOG.md
git commit -m "Log a refused dashboard request's reason beside its status.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 3: Tell a New task for a done module that it is done, and how to add work

**Files:**
- Modify: `agentflow/backlog.py`: `_new_module_fields` (lines 230-253) and its one caller in `add` (line 287)
- Modify: `CHANGELOG.md` (the same `### Fixed` list)
- Test: `agentflow/tests/test_backlog.py` (next to `test_a_new_module_needs_a_fresh_id_and_dependencies_already_in_the_map`, line 480)

**Interfaces:**
- Consumes: `finished.recorded(repo) -> dict[str, str]` (`{module: reason}` for every module done now, files only), `finished.plan_complete(branch)`, `finished.SPEC_SAYS`, `finished.BY_HAND`, `finished.MERGED`, and `cfg.project.fork_from`.
- Produces: `_new_module_fields(value, nodes, book, noun="capability map", done=None)`, where `done: dict[str, str] | None` is `finished.recorded(repo)`, passed by `add`. A module that isn't done keeps today's messages unchanged.

- [ ] **Step 1: Write the failing test** (parametrized over the reason, so the test seeds `runs/queue.json` itself and doesn't depend on how the fixture's module became done)

```python
@pytest.mark.parametrize("reason, says", [
    ("plan complete on main", "add unticked `- [ ]` steps for the new work to its plan on main"),
    ("spec says done", "run `agentflow backlog undone a`"),
    ("done by hand", "run `agentflow backlog undone a`"),
    ("merged", None),
])
def test_a_new_task_named_after_a_done_module_says_it_is_done_and_how_to_add_work(sbk, monkeypatch, reason, says):
    monkeypatch.setattr(backlog.finished, "recorded", lambda repo: {"a": reason})
    with pytest.raises(Invalid) as caught:
        backlog.add(sbk.cfg, sbk.root, "feature", {"new_module": {"module": "a", "responsibility": "more", "deps": []}})
    message = str(caught.value)
    assert message.startswith(f"a is already done ({reason}). Give the new work its own module id")
    assert "start module:a" not in message  # a done module has no Start button
    assert (says in message) if says else ", or reopen a:" not in message
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --no-project --with pyyaml --with pytest pytest "agentflow/tests/test_backlog.py::test_a_new_task_named_after_a_done_module_says_it_is_done_and_how_to_add_work" --import-mode=prepend -q`
Expected: 4 failed (the message is `a is already a module in the spec files: start module:a instead`).

- [ ] **Step 3: Implement it in `agentflow/backlog.py`**

In `add`, pass `done=finished.recorded(repo)` to `_new_module_fields`. In `_new_module_fields`, put this check before the existing `if module in in_map:` branch: when `module in in_map and module in done`, raise `Invalid` with this exact copy (the `reopen` hint is chosen by `reason`):

- `reason.startswith(finished.plan_complete(""))`: `add unticked \`- [ ]\` steps for the new work to its plan on {branch}, and it comes back to Backlog`, where `branch` is the text after `"plan complete on "`
- `reason in (finished.SPEC_SAYS, finished.BY_HAND)`: `run \`agentflow backlog undone {module}\``
- otherwise (`merged`, or anything else): no hint

```python
message = f"{module} is already done ({reason}). Give the new work its own module id"
raise Invalid(message + (f", or reopen {module}: {hint}" if hint else ""))
```

- [ ] **Step 4: Add the changelog line** to the same `### Fixed` list:

```markdown
- A New task whose module id is a module that's already done says so and how to add work to it (unticked plan steps, `agentflow backlog undone`, or a new id), not "start module:<id> instead" for a module with no Start button.
```

- [ ] **Step 5: Run the backlog and dashboard tests**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_backlog.py agentflow/tests/test_dashboard.py agentflow/tests/test_portability.py --import-mode=prepend -q`
Expected: all pass, including the existing `test_a_new_module_needs_a_fresh_id_and_dependencies_already_in_the_map` (it accepts `Invalid` or `Refused`).

- [ ] **Step 6: Commit**

```bash
git add agentflow/backlog.py agentflow/tests/test_backlog.py CHANGELOG.md
git commit -m "Say that a New task's module is already done, and how to add work to it.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Verification

- [ ] Full suite: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`. Expected: all pass (25-60 min). `git diff --stat agentflow/tests/fixtures/explain` is empty.
- [ ] `bin/agentflow validate`. Expected: `0 warnings`.
- [ ] Refresh the installed tool that the other repository runs: `uv tool install --reinstall --from . agentflow`.
- [ ] By hand, in a repository with a New task form (this one works), run `agentflow dashboard` and open the printed URL:
  - Press **+ New task**, choose Feature, enter module id `Bad Id` with any "What it does", and press OK. Expected: the dialog stays open, red text under the fields reads `module id 'Bad Id' must be a lowercase slug (letters, digits, - and _)`, and the terminal prints `dashboard: POST /api/backlog -> 400: module id 'Bad Id' must be …`.
  - Change the id to `good-id` and press OK. Expected: the dialog closes, and the floating toast reads `Added to Backlog: press Start or drag it to define`.
  - Reopen **+ New task**. Expected: no leftover red text.
  - Enter a Feature whose module id is a module in the Backlog's Done list with the reason `plan complete on main`. Expected red text: `<id> is already done (plan complete on main). Give the new work its own module id, or reopen <id>: add unticked \`- [ ]\` steps for the new work to its plan on main, and it comes back to Backlog`.
