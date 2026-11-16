# Dashboard slice U3 (Agents) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every agent of every run, and what it did. A run's Agents tab lists every session the run started, labelled in words, with its model, effort, cost, exit and result. Each one opens a transcript viewer that shows turns, not raw JSON, and follows a live session as it works. Board cards show every live agent, and the global Agents tab groups agents by run and links each one to its transcript.

**Architecture:**
- **Server.**
  - A new read-only module, `agentflow/sessions.py`, builds the run's sessions in one pass over `events.jsonl`. Each `agent_started` event is one session with a stable id `s<seq>`, joined with its `agent_session`, `agent_turn`, `agent_exited` and `subagent_*` events and with the step result that followed it.
    - The session knows its transcript file, `transcripts/<safe_name(key)>.<attempt>.jsonl`, so no route ever takes a file name from the client.
  - A new module, `agentflow/transcripts.py`, turns transcript lines (Claude's `stream-json` and Codex's `--json`) into turns:
    - text, thinking, one line per tool call, tool results, session starts and results;
    - waits (`tool_progress`, `system`, `rate_limit_event`) folded into one line.
  - It serves the transcript in byte-cursor pages capped at `dashboard.artifact_max_kb`, reading through U2's `run_files` rules.
- **Page.**
  - The Run page gains an **Agents** tab (`#/run/<id>/agents[/<session>]`).
  - The transcript viewer pages forward, attaches tool results to their calls, and follows a live session on the quiet stream's `agent_activity`.
  - Cards list every live agent.
  - The global Agents tab groups its rows by run, puts idle agents first, and links each row to its transcript.

**Tech Stack:** Python 3.12 stdlib, pytest, plain browser JavaScript (no framework, no build step), and node (tests only, when on `PATH`).

**Spec:** `docs/specs/agentflow-dashboard-ux-proposal.md` (`Status: approved`, 2026-11-15). This plan covers slice **U3** (§7):
- §4.5;
- §4.8;
- §4.10's "a live session streams new turns";
- §4.11's `sessions` and `transcript` routes;
- §9's transcript risk (paginate, cap, cache);
- Verification "U3".

**Builds on U1 and U2**, which must be merged first:
- from U1: `artifacts.artifact_max_bytes`, `NotFound` and `ARTIFACT_ROUTE`;
- from U2: `run_files._head`'s read rules, which this plan extends to a byte range;
- the node `js` helper in `conftest.py`;
- U2's Files tab, whose "Transcript tail" section this slice removes.

## Global Constraints

- Engine dependencies stay stdlib + `pyyaml>=6`. No JavaScript framework, bundler or CDN. node is a test-time tool only, and a test that needs it is skipped when `shutil.which("node")` is None.
- `PAGE_CSP` stays byte-identical.
- Every new route is GET only and needs the token, the Host check and the Origin check. Run ids go through `_run()`. A session id must `fullmatch` `^s\d{1,9}$` and be one of the run's sessions. The transcript path comes from the session record, never from the request.
- Transcript files are read under `views._tail`'s rules: no symlink (on the file or on `transcripts/`), resolved inside the run directory, opened `O_NOFOLLOW`, regular files only.
- Every string from a run is set with `textContent`. The page sink scan, which covers every `dashboard_ui/*.js`, refuses these literals: `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval(`, `new Function`, `setTimeout("`, ` style="` and `javascript:`. It also refuses any `https?://` match.
- No project strings in the engine (`test_portability.py` `DENYLIST`). Don't name the repository the spec measured: call it "the measured repository" in code, tests, docs and commits.
- No runner behaviour changes and no new config key. The transcript page cap is `dashboard.artifact_max_kb`, which §8 defines as "the largest artifact, transcript page or diff the page loads".
- The global Agents tab's "Exited" window stays `dashboard.agents.show_exited_min` (10 min by default), because the per-run tab now holds the history (§4.8).
- The golden files `agentflow/tests/fixtures/explain/*.txt` stay byte-identical.
- `CHANGELOG.md` gets one line under `## [Unreleased]` for each user-visible change, as each task says.
- Never create, edit or delete `uv.lock`. Run everything with `uv run --no-project`.
- Test command: `uv run --no-project --with pyyaml --with pytest pytest <path> --import-mode=prepend -q`.
- A commit message is one imperative sentence and ends with the pack's `project.commit_trailer`.

## Review Focus

1. **One transcript line larger than the page cap**, for example a 3 MB tool result. The page must not stall on it or load it whole. It becomes one `skipped` turn ("a record of 3.0 MB was left out"), and the cursor moves past it. The test goes in Task 2.
2. **A transcript still being written**, whose last line has no newline yet. That line isn't consumed: `next` stops before it, and the next page reads it once it is complete. The test goes in Task 2.
3. **A sizer and attempt 1 share `<key>.1.jsonl`**, because the sizer writes to `transcript_path(key, 1)`. Both rows open the same file, with distinct labels ("· sizer", "· attempt 1"). Each `system`/`init` record starts a "Session started" turn, so the file reads as two sessions in a row. The tests go in Task 1 and Task 2.
4. **A session that died before `system`/`init`**, so it has no `session_id`. Its `agent_exited` is matched by `pid`. Its row shows the exit code, and a missing transcript reads "No transcript was written", not a 404 toast. The test goes in Task 1.
5. **A line that isn't JSON, or a record type the parser doesn't know** (a newer CLI, or Codex's other items). It becomes a capped `raw` turn. It is never a 500, and never silently dropped. The test goes in Task 2.

---

### Task 1: Every session of a run

**Files:**
- Create: `agentflow/sessions.py`.
- Test: `agentflow/tests/test_sessions.py`.

**Interfaces:**
- Consumes: `state.safe_name`, `state.cost_added`, `views.step_title`, `views._effort_reason`, `views._alive`, `autonomy.RESOLVE_MARK` and `Run.transcript_path`.
- Produces:
  - `SESSION_ID = re.compile(r"^s\d{1,9}$")`. `SUMMARY_MAX = 200`.
  - `run_sessions(run: Run, events: list[dict] | None = None) -> list[dict]`. It returns newest first, one row per `agent_started` event:
    ```
    {id, key, label, phase, role, agent, model, effort, effort_reason, sizer, attempt, session_id, pid,
     started_at, ended_at, duration_s, cost, exit_code, live, context_pct, last_tool, summary,
     transcript, subagents: [{subagent_type, model, done}]}
    ```
    - **`id`** is `f"s{seq}"` of the `agent_started` event. **`key`** is its `step` (or `key`).
    - **`attempt`** counts the `agent_started` events of that key without `sizer: true`, up to and including this one. A sizer gets `attempt: 1` and `sizer: True`.
    - **`label`** is `step_title(key)`, then:
      - `" · sizer"` for a sizer;
      - otherwise `" · attempt <n>"`, unless the key holds `RESOLVE_MARK` and `n == 1`.
      - So the labels read `Task 9 · implement · attempt 2` and `Task 9 · implement · resolver 1`.
    - **`session_id`** is the `agent_started.session_id`, or the first later `agent_session` with the same `pid` (or the same `key` when there is no pid).
    - **`agent_exited`** is matched by `session_id`, else by `pid`, taking the first unmatched exit after the start.
      - `cost` is that exit's `cost_added` (one `counted` dict for the whole pass, as `state` does), rounded to 4 places.
      - `ended_at` and `exit_code` come from it, and both are `None` while there's no exit.
    - **`live`**: no exit yet, and `views._alive(pid)`. Call it through the module (`from agentflow import views`), so a test's `monkeypatch` of `agentflow.views._alive` reaches it.
    - **`context_pct`** and **`last_tool`** come from the last `agent_turn` with this `session_id` between the start and the end.
    - **`summary`** is the first line of the first `step_finished.summary` or `hitl_raised.summary` for `key` after the start and before the next start of the same key, cut to `SUMMARY_MAX` characters. `""` when there is none.
    - **`transcript`** is True when `run.transcript_path(key, attempt)` exists as a regular file reached without a symlink.
    - **`subagents`** come from the `subagent_started`/`subagent_finished` events whose `parent_session` is this `session_id`, between the start and the end.
  - `find_session(run: Run, sid: str) -> dict`: the row with that `id`, else `artifacts.NotFound("no session <sid> in this run")`. A malformed `sid` is `NotFound` too.
  - `transcript_file(run: Run, session: dict) -> Path`, which is `run.transcript_path(session["key"], session["attempt"])`.

- [ ] **Step 1: Write the failing tests**

```python
def started(run, key, pid, **kw):
    return run.emit("agent_started", step=key, pid=pid, agent="claude", model=kw.get("model", "sonnet"),
                    effort="high", role="build", session_id=kw.get("session_id"), sizer=kw.get("sizer", False))

def test_three_sessions_on_one_step_read_as_three_labels(flow_repo):     # spec Verification U3
    run, k = flow_repo.new_run(), "build/build/task-9/implement"
    for n, pid in ((1, 11), (2, 12)):
        started(run, k, pid); run.emit("agent_session", session_id=f"a{n}", pid=pid, key=k, model="sonnet")
        run.emit("agent_exited", session_id=f"a{n}", exit_code=0 if n == 2 else 1, cost=0.5 * n, key=k)
        run.emit("step_finished", key=k, status="done" if n == 2 else "failed", summary=f"try {n}\nmore")
    started(run, f"{k}/resolve-1", 13, model="opus")
    run.emit("agent_session", session_id="r1", pid=13, key=f"{k}/resolve-1", model="opus")
    labels = [s["label"] for s in run_sessions(run)]
    assert labels == ["Task 9 · implement · resolver 1", "Task 9 · implement · attempt 2",
                      "Task 9 · implement · attempt 1"]
    rows = {s["label"]: s for s in run_sessions(run)}
    assert rows["Task 9 · implement · attempt 1"]["exit_code"] == 1
    assert rows["Task 9 · implement · attempt 1"]["summary"] == "try 1"
    assert rows["Task 9 · implement · attempt 2"]["cost"] == 1.0

def test_a_sizer_shares_attempt_ones_file_with_its_own_label(flow_repo):  # Review Focus 3
    run, k = flow_repo.new_run(), "plan/plan"
    started(run, k, 21, sizer=True); started(run, k, 22)
    sizer, first = sorted(run_sessions(run), key=lambda s: s["id"])
    assert (sizer["label"], first["label"]) == ("plan · sizer", "plan · attempt 1")
    assert transcript_file(run, sizer) == transcript_file(run, first) == run.transcript_path(k, 1)

def test_a_session_that_died_before_init_is_matched_by_pid(flow_repo):     # Review Focus 4
    run, k = flow_repo.new_run(), "define/draft"
    started(run, k, 31)
    run.emit("agent_exited", pid=31, exit_code=137, key=k)
    (row,) = run_sessions(run)
    assert row["session_id"] is None and row["exit_code"] == 137 and row["transcript"] is False and not row["live"]

def test_find_session_refuses_what_is_not_a_session(flow_repo):
    run = flow_repo.new_run(); started(run, "plan/plan", 41)
    for sid in ("s999", "../x", "1", "s1/..", ""):
        with pytest.raises(NotFound):
            find_session(run, sid)

def test_a_resumed_session_counts_its_cost_once(flow_repo):
    run, k = flow_repo.new_run(), "build/build/task-1/implement"
    for cost in (0.4, 1.0):  # the second call resumes the same session: total_cost_usd spans both
        started(run, k, 51, session_id="same"); run.emit("agent_exited", session_id="same", exit_code=0, cost=cost, key=k)
    assert sorted(s["cost"] for s in run_sessions(run)) == [0.4, 0.6]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_sessions.py --import-mode=prepend -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'agentflow.sessions'`.

- [ ] **Step 3: Implement `sessions.py`**

Make one forward pass over the events. Keep the open sessions in two indexes, by `session_id` and by `pid`, so a matched exit closes exactly one row.

- [ ] **Step 4: Run the tests to verify they pass**

Same command, plus `test_portability.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentflow/sessions.py agentflow/tests/test_sessions.py
git commit -m "List every agent session of a run with its label, model, cost, exit and result."
```

---

### Task 2: Transcripts as turns, in capped pages

**Files:**
- Create: `agentflow/transcripts.py`.
- Modify: `agentflow/run_files.py`, adding `read_range`.
- Test: `agentflow/tests/test_transcripts.py`.

**Interfaces:**
- Consumes: U2's `run_files._head` rules, Task 1's `find_session` and `transcript_file`, and `artifacts.NotFound`.
- Produces:
  - `run_files.read_range(root: Path, path: Path, start: int, limit: int) -> tuple[bytes, int] | None` returns `(bytes from start, file size)`, with `_head`'s rules. It returns `None` when a rule fails or the file is missing.
  - Constants: `TEXT_MAX = 8 * 1024` (one turn's text), `IO_MAX = 4 * 1024` (a tool's input or result), and `SUMMARY_MAX = 200` (a tool line).
  - `WAIT_TYPES = ("tool_progress", "rate_limit_event", "stream_event")`. A `system` record whose subtype isn't `init` is also a wait.
  - `parse_lines(lines: list[bytes]) -> list[dict]` is pure. Each turn has a `kind`:
    - `{kind: "session", model}`: a Claude `system`/`init`, or a Codex `thread.started`.
    - `{kind: "text", text, parent}`: an assistant `text` block, or a Codex `agent_message`.
    - `{kind: "thinking", text, parent}`: a `thinking` block, or a Codex `reasoning` item.
    - `{kind: "tool", id, name, line, input, parent}`: a `tool_use` block, or a Codex item of any other type.
      - `line` is the tool line: `Bash: <command>`, `Read`/`Edit`/`Write: <file_path>`, `Grep`/`Glob: <pattern>`, `Agent`/`Task: <description>`, otherwise `<name>: <first string value of the input>`. It is cut to `SUMMARY_MAX` characters.
      - `input` is the input as indented JSON, cut to `IO_MAX` characters.
      - For Codex, `command_execution` gives `name` `"command"` and the line `"command: " + item.command`, and its `aggregated_output` becomes an inline `result`.
    - `{kind: "tool_result", id, text, is_error, parent}`: a `user` `tool_result` block. Its content, a string or a list of text blocks, is joined and cut to `IO_MAX`.
    - `{kind: "wait", records, seconds}`: a run of wait records next to each other, as one turn. `seconds` is the largest `elapsed_time_seconds` in the run, or `None`.
    - `{kind: "result", text, cost, is_error}`: a Claude `result` (`result`, `total_cost_usd`, `is_error`).
    - `{kind: "raw", text}`: a line that isn't JSON, or a type not listed here, cut to `IO_MAX`.
    - Every cut text ends with `" … (<n> more characters)"`. `parent` is the line's `parent_tool_use_id` (a subagent's turn) or `None`.
  - `transcript_page(run: Run, sid: str, after: int, max_bytes: int) -> dict` returns `{session, turns, after, next, size, done}`.
    - It reads `read_range(run.dir, transcript_file(...), after, max_bytes)` and parses only the complete lines, so `next` points just after the last `\n` it consumed.
    - A file that is missing, or breaks a read rule, gives `{turns: [], next: 0, size: 0, done: True}` with `missing: True`.
    - **An oversized line**: when one line is longer than `max_bytes`, so no `\n` falls in the read, the server looks for that line's end in 64 KB steps without keeping the bytes. It emits `{kind: "skipped", bytes}` and sets `next` after the line.
    - **Pages are always whole lines**, so a bad `after` can't split a record: an `after` that isn't `0` and isn't just after a `\n` in the file is a `ValueError("after must be a page boundary")`. So is an `after` past the size.
    - `done` is `next == size` and the session isn't `live`.
  - **Cache:** parsed pages are kept by `(path, after, max_bytes, size, mtime_ns)`, at most 64 entries.

- [ ] **Step 1: Write the failing tests**

```python
def L(obj) -> bytes: return json.dumps(obj).encode()

def test_turns_read_like_a_conversation():
    turns = parse_lines([
        L({"type": "system", "subtype": "init", "model": "sonnet", "session_id": "a"}),
        L({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "Running the tests."},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest tests/x.py -q"}}]}}),
        L({"type": "tool_progress", "elapsed_time_seconds": 12}), L({"type": "tool_progress", "elapsed_time_seconds": 40}),
        L({"type": "system", "subtype": "hook_started"}), L({"type": "rate_limit_event"}),
        L({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "3 passed"}]}}),
        L({"type": "result", "result": "Task 9 is complete", "total_cost_usd": 0.88, "is_error": False})])
    assert [t["kind"] for t in turns] == ["session", "thinking", "text", "tool", "wait", "tool_result", "result"]
    assert turns[3]["line"] == "Bash: pytest tests/x.py -q" and turns[4] == {"kind": "wait", "records": 4, "seconds": 40}
    assert turns[5]["text"] == "3 passed" and turns[6]["cost"] == 0.88

def test_codex_items_and_unknown_lines_become_turns():                      # Review Focus 5
    turns = parse_lines([L({"type": "thread.started", "thread_id": "c"}),
                         L({"type": "item.completed", "item": {"type": "command_execution", "command": "ls",
                                                                "aggregated_output": "a\nb"}}),
                         b"not json", L({"type": "brand_new_type", "x": 1})])
    assert [t["kind"] for t in turns] == ["session", "tool", "raw", "raw"] and turns[1]["line"] == "command: ls"

def test_a_file_holding_a_sizer_and_attempt_one_reads_as_two_sessions():   # Review Focus 3
    init = L({"type": "system", "subtype": "init", "model": "haiku"})
    assert [t["kind"] for t in parse_lines([init, L({"type": "result", "result": "high"}), init])] == \
        ["session", "result", "session"]

def test_pages_return_every_line_of_a_5_mb_transcript(flow_repo):         # spec Verification U3
    run, sid = session_with_transcript(flow_repo, lines=20000, width=250)   # about 5 MB of assistant text turns
    after, seen = 0, 0
    while True:
        page = transcript_page(run, sid, after, 512 * 1024)
        assert page["next"] - page["after"] <= 512 * 1024
        seen += sum(1 for t in page["turns"] if t["kind"] == "text")
        after = page["next"]
        if page["done"]: break
    assert seen == 20000

def test_a_line_larger_than_the_page_is_skipped_not_stuck(flow_repo):      # Review Focus 1
    run, sid = session_with_transcript(flow_repo, raw=[L({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "x" * (3 << 20)}]}}), L({"type": "result", "result": "ok"})])
    first = transcript_page(run, sid, 0, 512 * 1024)
    assert first["turns"][0]["kind"] == "skipped" and first["turns"][0]["bytes"] > 3 << 20
    rest = transcript_page(run, sid, first["next"], 512 * 1024)
    assert [t["kind"] for t in first["turns"] + rest["turns"]] == ["skipped", "result"]

def test_a_line_still_being_written_waits_for_its_newline(flow_repo):      # Review Focus 2
    run, sid = session_with_transcript(flow_repo, raw=[L({"type": "result", "result": "a"})], tail=b'{"type": "res')
    page = transcript_page(run, sid, 0, 512 * 1024)
    assert len(page["turns"]) == 1 and page["next"] < page["size"]

def test_a_symlinked_transcript_or_a_bad_cursor_is_refused(flow_repo, tmp_path):
    run, sid = session_with_transcript(flow_repo, raw=[L({"type": "result", "result": "ok"})])
    path = transcript_file(run, find_session(run, sid))
    size = path.stat().st_size
    for after in (3, size + 1):
        with pytest.raises(ValueError, match="page boundary"):
            transcript_page(run, sid, after, 512 * 1024)
    (tmp_path / "secret.jsonl").write_bytes(L({"type": "result", "result": "secret"}) + b"\n")
    path.unlink(); path.symlink_to(tmp_path / "secret.jsonl")
    page = transcript_page(run, sid, 0, 512 * 1024)
    assert page["missing"] is True and page["turns"] == []
```

`session_with_transcript(flow_repo, lines=0, width=0, raw=None, tail=b"") -> tuple[Run, str]` is a helper in this test file. It emits one `agent_started` and one `agent_exited`, writes the transcript at `run.transcript_path(key, 1)`, and returns the run and the session id.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_transcripts.py --import-mode=prepend -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `read_range` and `transcripts.py`**

To check the page boundary, read the one byte before `after` (`read_range(..., after - 1, 1)`) and require `b"\n"`.

- [ ] **Step 4: Run the tests to verify they pass**

Same command, plus `test_run_files.py` and `test_portability.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentflow/transcripts.py agentflow/run_files.py agentflow/tests/test_transcripts.py
git commit -m "Read agent transcripts as conversation turns in capped whole-line pages, folding the waits."
```

---

### Task 3: The `sessions` and `transcript` routes

**Files:**
- Modify: `agentflow/dashboard.py`:
  - `ARTIFACT_ROUTE` gains `sessions`;
  - a new `TRANSCRIPT_ROUTE = re.compile(r"^/api/runs/([^/]+)/sessions/([^/]+)/transcript$")`, matched before `ARTIFACT_ROUTE`;
  - the docstring.
- Modify: `agentflow/views.py`:
  - `run_detail` gains `session_count` (the count of `agent_started` events in the log it already reads);
  - it drops `transcript` and `transcript_file`, which the Agents tab replaces.
- Modify: `agentflow/dashboard_ui/files.js`, which drops U2's "Transcript tail" section, and `agentflow/tests/test_dashboard.py`. Replace the `transcript` assertions in `test_board_and_run_detail` (line 318) with `session_count`. The tail's 16 KB cap now lives in the page cap test.
- Test: `agentflow/tests/test_dashboard.py`.

**Interfaces:**
- Consumes: `sessions.run_sessions` (Task 1), `transcripts.transcript_page` (Task 2) and `artifacts.artifact_max_bytes` (U1).
- Produces:
  - `GET /api/runs/<id>/sessions` → `run_sessions(run)`.
  - `GET /api/runs/<id>/sessions/<sid>/transcript?after=<n>` → `transcript_page(run, sid, n, artifact_max_bytes(cfg))`.
    - `after` defaults to `0`.
    - A non-integer, a negative or a doubled `after` → 400.
    - `ValueError` → 400. `NotFound` → 404.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_agents_routes_answer(dash):
    run, sid = session_with_transcript(dash.flow, raw=[L({"type": "result", "result": "ok"})])
    (row,) = payload(dash.get(f"/api/runs/{run.id}/sessions")[2])
    assert row["id"] == sid and row["transcript"] is True
    page = payload(dash.get(f"/api/runs/{run.id}/sessions/{sid}/transcript")[2])
    assert [t["kind"] for t in page["turns"]] == ["result"] and page["done"] is True
    assert payload(dash.get(f"/api/runs/{run.id}")[2])["session_count"] == 1
    assert "transcript" not in payload(dash.get(f"/api/runs/{run.id}")[2])

@pytest.mark.parametrize("route", ["sessions", "sessions/s1/transcript", "sessions/s1/transcript?after=0"])
def test_the_agents_routes_keep_the_refusal_matrix(dash, route):
    run, _ = session_with_transcript(dash.flow, raw=[L({"type": "result", "result": "ok"})])
    assert_refusal_matrix(dash, run, route)

@pytest.mark.parametrize("tail, status", [("sessions/s999/transcript", 404), ("sessions/..%2F..%2Fstate.json/transcript", 404),
                                          ("sessions/s1/transcript?after=-1", 400), ("sessions/s1/transcript?after=x", 400),
                                          ("sessions/s1/transcript?after=0&after=1", 400)])
def test_the_transcript_route_refuses_bad_ids_and_cursors(dash, tail, status):
    run, _ = session_with_transcript(dash.flow, raw=[L({"type": "result", "result": "ok"})])
    assert dash.get(f"/api/runs/{run.id}/{tail}")[0] == status
```

Move `session_with_transcript` and `L` into `conftest.py`, so both test files use them. `assert_refusal_matrix` is U2's helper.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py -k "agents_routes or transcript_route or board_and_run_detail" --import-mode=prepend -q`
Expected: FAIL.

- [ ] **Step 3: Add the routes, `session_count`, and drop the tail**

Add to the docstring's route list: "`GET /api/runs/<id>/sessions` (every agent session of the run) and `/sessions/<s…>/transcript?after=<byte>` (its transcript as turns, in whole-line pages of at most `dashboard.artifact_max_kb`)".

- [ ] **Step 4: Run the tests to verify they pass**

Run all of `test_dashboard.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentflow/dashboard.py agentflow/views.py agentflow/dashboard_ui/files.js agentflow/tests
git commit -m "Serve a run's agent sessions and their transcripts in pages, and drop the 16 KB transcript tail."
```

---

### Task 4: The Run page's Agents tab and the transcript viewer

**Files:**
- Create: `agentflow/dashboard_ui/sessions.js`, added to `UI_SCRIPTS` after `files.js`.
- Modify:
  - `agentflow/dashboard_ui/run.js`:
    - `RUN_TABS` becomes `["overview", "spec", "plan", "agents", "activity", "gates", "files"]`, and the Agents tab's label is `"Agents (" + detail.session_count + ")"`;
    - `runTabEntries` hands `agents` to `sessionEntries`;
    - `attachNode` moves onto live session rows.
  - `agentflow/dashboard_ui/main.js`: `onStreamMessage` feeds an open live transcript.
  - `agentflow/dashboard.html`: the session list, turn and fold styles.
- Modify: `docs/agentflow-playbook.md`, line 329. Add one sentence on the Agents tab and the transcript viewer.
- Modify: `CHANGELOG.md`.
- Test: `agentflow/tests/test_dashboard_js.py` and `agentflow/tests/test_dashboard.py`.

**Interfaces:**
- Consumes: Task 3's routes, `patchChildren`, `entry`, `throttle`, `money`, `dur`, `ago`, `ui.run`, `boardRefreshMs` and `attachNode`.
- Produces, in `sessions.js`. The pure helpers sit at the top.
  - `filterSessions(rows, f: {phase: string|null, role: string|null, failed: boolean}) -> rows`. `failed` keeps rows whose `exit_code` is set and isn't `0`.
  - `groupSessions(rows) -> [{phase, rows}]`. Live rows come first in a group with phase `"live"`, then one group per phase, in the order of each phase's newest session. Rows keep their newest-first order.
  - `mergeTurns(have: Turn[], page: Turn[]) -> Turn[]`. It attaches each `tool_result` to the `tool` with the same `id`, as `result` and `is_error`, whichever page the tool came from. A `tool_result` with no `tool` stays as its own turn. Consecutive `wait` turns merge, summing `records` and keeping the larger `seconds`.
  - `waitText(turn) -> string`: `"waited 40 s"` when `seconds` is set, else `"waited (4 updates)"`.
  - **UI state:** `ui.sessions = {runId, rows, filter, open: sid|null, turns: Turn[], next, size, done, missing}`.
  - `sessionEntries(detail, sub) -> entry[]`, where `sub` is a session id from `#/run/<id>/agents/<sid>`.
    - Without `sub`, it draws these parts:
      - a header: "Agents · `<N>` sessions · `<live>` live · `money(total)`", and the phase, role and failed-only filters;
      - one group heading per `groupSessions` group;
      - one row per session, keyed `session:<id>`, showing: a status mark (`● live`, `✓ exit 0` or `✗ exit <n>`), the label, model · effort (with the reason as its tooltip), `dur(duration_s)` (or `ago(started_at)` while live), `money(cost)`, `ctx <n>%`, the quoted `summary`, and for live rows the `last_tool` and the attach box.
      - A click on a row goes to `#/run/<id>/agents/<sid>`.
    - With `sub`, it draws the viewer:
      - a "← All sessions" link and the session's row as a header;
      - the turns, keyed `turn:<index>`:
        - `text` is a paragraph;
        - `thinking` is a closed `<details>` reading "Thinking";
        - `tool` is a `<details>` whose summary is the tool line plus `✗` on an error, and whose body holds the input and the result `<pre>`s;
        - `wait` is a muted line from `waitText`;
        - `result` is a box with the cost;
        - `skipped`: "A record of `<n>` MB was left out";
        - `raw` is a muted `<pre>`;
        - a turn with a `parent` is indented under its `Agent`/`Task` call.
      - At the end: **Load more (`<size − next>` KB left)** while `!done && next < size`. "No transcript was written." when `missing`. "Following this session live" while the session is live and caught up.
  - **Following a live session.** In `main.js`, an `agent_activity` message whose `run_id` is the open run and whose `session_id` is the open session's `session_id` calls `transcriptFollow()`. That function fetches `?after=<next>` through `throttle(…, () => boardRefreshMs)` and appends with `mergeTurns`.
    - It only follows when the reader is within 200 px of the bottom.
    - Otherwise it shows "New turns below" and keeps the scroll where it is.

- [ ] **Step 1: Write the failing tests**

```python
# test_dashboard_js.py
def test_tool_results_attach_to_their_call_across_pages():
    have = js(["sessions.js"], "mergeTurns([], args)", [{"kind": "tool", "id": "t1", "line": "Bash: ls"},
                                                       {"kind": "wait", "records": 2, "seconds": 5}])
    merged = js(["sessions.js"], "mergeTurns(args.have, args.page)",
                {"have": have, "page": [{"kind": "wait", "records": 3, "seconds": 40},
                                        {"kind": "tool_result", "id": "t1", "text": "a b", "is_error": False},
                                        {"kind": "tool_result", "id": "zz", "text": "orphan", "is_error": True}]})
    assert merged[0]["result"] == "a b" and merged[1] == {"kind": "wait", "records": 5, "seconds": 40}
    assert [t["kind"] for t in merged] == ["tool", "wait", "tool_result"]
    assert js(["sessions.js"], "waitText(args)", {"kind": "wait", "records": 4, "seconds": None}) == "waited (4 updates)"

def test_sessions_group_live_first_then_by_phase_and_filter():
    rows = [{"id": "s9", "phase": "build", "live": True, "exit_code": None, "role": "build"},
            {"id": "s8", "phase": "build", "live": False, "exit_code": 1, "role": "build"},
            {"id": "s5", "phase": "plan", "live": False, "exit_code": 0, "role": "plan"},
            {"id": "s3", "phase": "build", "live": False, "exit_code": 0, "role": "review"}]
    groups = js(["sessions.js"], "groupSessions(args).map(g => [g.phase, g.rows.map(r => r.id)])", rows)
    assert groups == [["live", ["s9"]], ["build", ["s8", "s3"]], ["plan", ["s5"]]]
    failed = js(["sessions.js"], "filterSessions(args, {phase: null, role: null, failed: true}).map(r => r.id)", rows)
    assert failed == ["s8"]

# test_dashboard.py
def test_the_run_page_has_an_agents_tab_with_a_transcript_viewer():
    run_js = (UI_DIR / "run.js").read_text(encoding="utf-8")
    assert 'RUN_TABS = ["overview", "spec", "plan", "agents", "activity", "gates", "files"]' in run_js
    s = (UI_DIR / "sessions.js").read_text(encoding="utf-8")
    for needle in ("/sessions", "/transcript?after=", "Load more", "No transcript was written",
                   "Following this session live", "New turns below", "ui.sessions"):
        assert needle in s, needle
    main = (UI_DIR / "main.js").read_text(encoding="utf-8")
    assert "transcriptFollow(" in main
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard_js.py agentflow/tests/test_dashboard.py -k "attach_to_their_call or group_live_first or agents_tab_with" --import-mode=prepend -q`
Expected: FAIL.

- [ ] **Step 3: Implement `sessions.js`, the tab and the follow**

- [ ] **Step 4: Run the tests, then check in a browser**

Run `test_dashboard.py` and `test_dashboard_js.py`. Expected: PASS, with no node test skipped.

In a browser on `bin/agentflow dashboard`, using U0's scratch-state agent-turn script (U0 plan, Task 8 Step 5), with the script also appending assistant lines to the session's transcript:
- `#/run/<id>/agents` lists the session as live, and a reload keeps the tab;
- opening it shows the turns and follows new ones live, at most once per `board_refresh_s`;
- scrolling up shows "New turns below" without jumping;
- a finished run with several attempts lists each one with its own label, and **Load more** reaches the end of a large transcript.

- [ ] **Step 5: Add the changelog line and commit**

Under `### Added`: "A run's Agents tab lists every session the run started (attempts, resolvers and sizers labelled in words) with model, effort, cost, exit and result, and opens each one's transcript as a readable conversation that follows a live session."
Under `### Removed`: "The run detail's 16 KB transcript tail (`transcript`, `transcript_file`); the Agents tab's transcript viewer replaces it."

```bash
git add agentflow/dashboard_ui agentflow/dashboard.html agentflow/dashboard.py agentflow/tests docs/agentflow-playbook.md CHANGELOG.md
git commit -m "Add the run's Agents tab and a transcript viewer that follows a live session."
```

---

### Task 5: Multi-agent cards and the global Agents tab by run

**Files:**
- Modify: `agentflow/views.py`:
  - `_card` gains `agents`;
  - `_agent_row` gains `label` and `session`;
  - `agents_view` reads each run's events once and passes them to both `_digest` and `run_sessions`.
- Modify: `agentflow/dashboard_ui/board.js`: a card with two or more live agents shows them all.
- Modify: `agentflow/dashboard_ui/agents.js`: rows are grouped by run, idle rows come first, and each row links to its session.
- Modify: `CHANGELOG.md`.
- Test: `agentflow/tests/test_dashboard.py` and `agentflow/tests/test_dashboard_js.py`.

**Interfaces:**
- Consumes: `sessions.run_sessions(run, events)` (Task 1). `sessions` imports `views`, so `views` imports `run_sessions` inside `_card` and `agents_view`, never at the top. That's the same pattern `doubt.py` uses for `autonomy`.
- Produces:
  - `_card(...)["agents"]`: `[{label, model, effort, arrow, reason, session}]`, one per live agent, in start order. `agent` stays as it is (the newest), for the peek.
  - Each `agents_view` row gains:
    - `session`, the id of the run's session with the same `session_id` (else the same `pid`), or `None`;
    - `label`, that session's label, else `step_title(key)`.
  - **Page, `board.js`.** When `card.agents.length > 1`, the card's agent line reads `"<n> agents: " + labels.join(", ")`, with each agent's model · effort as its tooltip. With one or none, the line is unchanged.
  - **Page, `agents.js`.** `agentsByRun(rows) -> [{run_id, module, rows}]` is a pure helper:
    - groups are in order of each run's first live row;
    - within a group, idle rows come first, then by `age_s` descending.
    - Each group has a header with the run's module and a link to `#/run/<id>/agents`.
    - A row's click goes to `#/run/<id>/agents/<session>` when `session` is set, else to the run.
    - An idle row shows `"idle " + ago(quiet) + " · " + last_tool`.
    - The "Exited in the last N min" table is grouped the same way.

- [ ] **Step 1: Write the failing tests**

```python
# test_dashboard.py
def test_a_card_lists_every_live_agent(dash, monkeypatch):
    run = awaiting(dash.flow)
    monkeypatch.setattr("agentflow.views._alive", lambda pid: True)
    for pid, key in ((61, "build/build/task-10/implement"), (62, "build/build/task-8/review")):
        run.emit("agent_started", step=key, pid=pid, agent="claude", model="sonnet", effort="high", role="build")
    assert [a["label"] for a in card_of(dash)["agents"]] == ["Task 10 · implement · attempt 1",
                                                              "Task 8 · review · attempt 1"]

def test_a_global_agent_row_links_to_its_session(dash, monkeypatch):
    run = awaiting(dash.flow)
    monkeypatch.setattr("agentflow.views._alive", lambda pid: True)
    ev = run.emit("agent_started", step="plan/plan", pid=71, agent="claude", model="opus", effort="high", role="plan")
    (row,) = payload(dash.get("/api/agents")[2])["live"]
    assert row["session"] == f"s{ev['seq']}" and row["label"] == "plan · attempt 1"

# test_dashboard_js.py
def test_agents_group_by_run_with_idle_rows_first():
    rows = [{"run_id": "r1", "module": "a", "idle": False, "age_s": 50}, {"run_id": "r2", "module": "b", "idle": False, "age_s": 9},
            {"run_id": "r1", "module": "a", "idle": True, "age_s": 10}]
    got = js(["agents.js"], "agentsByRun(args).map(g => [g.run_id, g.rows.map(r => r.age_s)])", rows)
    assert got == [["r1", [10, 50]], ["r2", [9]]]
```

If `agents.js` touches the DOM at load time, move those lines into functions first, so node can load it.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_dashboard.py agentflow/tests/test_dashboard_js.py -k "every_live_agent or links_to_its_session or group_by_run" --import-mode=prepend -q`
Expected: FAIL.

- [ ] **Step 3: Implement it**

- [ ] **Step 4: Run the tests, then check in a browser**

Run all of `test_dashboard.py` and `test_dashboard_js.py`. Expected: PASS.

In a browser, with two live agents on one run:
- the card reads "2 agents: …";
- the Agents tab groups them under the run;
- a click opens the session's transcript.

- [ ] **Step 5: Add the changelog line and commit**

Under `### Changed`: "A Board card lists every live agent of its run, not just the newest, and the Agents tab groups agents by run, puts idle ones first and opens each one's transcript."

```bash
git add agentflow/views.py agentflow/dashboard_ui agentflow/tests CHANGELOG.md
git commit -m "List every live agent on a card and group the Agents tab by run with links to each transcript."
```

---

### Task 6: Whole-slice verification

There's no new code in this task. If a check fails, fix it in the task that owns the code.

- [ ] **Step 1:** Run `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`. Expected: all pass, with no node test skipped on a machine that has node. This is the full suite, which takes 25–60 min.
- [ ] **Step 2:** Run `bin/agentflow validate`. Expected: `0 warnings`.
- [ ] **Step 3:** Run `git diff main -- agentflow/tests/fixtures/explain`. Expected: empty.
- [ ] **Step 4:** Run `git diff main -- agentflow/dashboard.py | grep -n PAGE_CSP`. Expected: no changed line.
- [ ] **Step 5:** Walk Verification "U3" from the spec in a browser, with a scratch `XDG_STATE_HOME`:
  - a step with attempt 1, attempt 2 and resolver 1 lists 3 rows with distinct labels;
  - a transcript shows parsed turns with the `tool_progress` lines folded into "waited …";
  - **Load more** walks a 5 MB transcript to its end;
  - a live session's new turns appear without a reload.
