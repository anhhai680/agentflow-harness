"""The dashboard's plan task board: its pure helpers, run with node and no DOM."""
from __future__ import annotations

import pytest
from conftest import NODE, js, js_async

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not on PATH")

T = lambda n, status, **kw: {"n": n, "status": status, "wave": kw.get("wave", 0), "blocks": kw.get("blocks", []),  # noqa: E731
                             "blocked_by": kw.get("blocked_by", []), "review": kw.get("review")}
PLAN = {"summary": {"total": 5, "committed": 1, "blocked": 1, "parked": 0, "running": 0, "failed": 0,
                    "waiting": 2, "independent": 1, "deferred": 0},
        "tasks": [T(1, "committed"), T(2, "blocked", wave=1, blocks=[3, 4]), T(3, "waiting", wave=2, blocked_by=[2]),
                  T(4, "waiting", wave=2, blocked_by=[2]), T(5, "independent", wave=1,
                  review={"findings": [{"text": "x"}]})]}


def test_the_summary_line_reads_like_the_spec():
    assert js(["plan.js"], "taskSummary(args)", PLAN) == \
        "5 tasks · 1 committed · 1 blocked · 2 waiting on it · 1 independent"


def test_ranges_compress_runs():
    assert js(["plan.js"], "ranges(args)", [10, 11, 12, 20, 22, 24]) == "10–12, 20, 22, 24"
    assert js(["plan.js"], "ranges(args)", [3, 4]) == "3, 4"
    assert js(["plan.js"], "ranges(args)", []) == ""


def test_grouping_and_filters():
    by_status = js(["plan.js"], "groupTasks(args, 'status').map(g => [g.label, g.tasks.map(t => t.n)])", PLAN["tasks"])
    assert [g[1] for g in by_status] == [[2], [3, 4], [5], [1]]
    by_wave = js(["plan.js"], "groupTasks(args, 'wave').map(g => [g.label, g.tasks.map(t => t.n)])",
                 PLAN["tasks"] + [T(6, "pending", wave=None)])
    assert by_wave == [["Wave 1", [1]], ["Wave 2", [2, 5]], ["Wave 3", [3, 4]], ["Cycle", [6]]]
    assert js(["plan.js"], "filterTasks(args, 'stopped').map(t => t.n)", PLAN["tasks"]) == [2]
    assert js(["plan.js"], "filterTasks(args, 'findings').map(t => t.n)", PLAN["tasks"]) == [5]


def test_the_plan_mode_follows_the_route():
    for sub, started, mode in [("tasks", False, "tasks"), ("task-12", False, "tasks"), ("document", True, "document"),
                               ("docs/plan.md", True, "document"), (None, True, "tasks"), (None, False, "document")]:
        assert js(["plan.js"], "planMode(args[0], args[1])", [sub, started]) == mode, (sub, started)


def test_a_task_row_reads_its_numbers():
    assert js(["plan.js"], "tries(args)", {"attempts": 1}) == "1 try"
    assert js(["plan.js"], "tries(args)", {"attempts": 3, "resolvers": 2}) == "3 tries · 2 resolvers"
    assert js(["plan.js"], "args.map(reviewText)", [None, {"skipped": True}, {"blocking": 2}, {"blocking": 0}]) == \
        ["—", "skipped", "2 blocking", "✓ 0"]
    assert js(["plan.js"], "statusText(args)", T(3, "waiting", blocked_by=[2])) == "○ waiting on 2"


FILES = [{"name": "issue.md", "group": "request", "size": 6}, {"name": "handoffs/plan.md", "group": "handoffs", "size": 2048},
         {"name": "handoffs/build.md", "group": "handoffs", "size": 20000}, {"name": "pr.md", "group": "pull request", "size": 10}]


def test_the_file_list_groups_in_order_and_sizes_read_short():
    groups = js(["files.js"], "groupFiles(args).map(g => [g.group, g.files.map(f => f.name)])", FILES)
    assert groups == [["request", ["issue.md"]], ["handoffs", ["handoffs/plan.md", "handoffs/build.md"]], ["pull request", ["pr.md"]]]
    assert js(["files.js"], "args.map(fileSize)", [0, 6, 2048, 20000, 3 * 1024 * 1024]) == ["0 B", "6 B", "2.0 KB", "19.5 KB", "3.0 MB"]


def test_the_chosen_file_defaults_to_the_first():
    assert js(["files.js"], "pickFile(args[0], args[1])", [FILES, None]) == "issue.md"
    assert js(["files.js"], "pickFile(args[0], args[1])", [FILES, "handoffs/build.md"]) == "handoffs/build.md"
    assert js(["files.js"], "pickFile(args[0], args[1])", [[], None]) is None


def test_the_changes_summary_totals_added_and_deleted():
    changes = {"files": [{"path": "a", "status": "M", "added": 3, "deleted": 1}, {"path": "b", "status": "A", "added": None, "deleted": None},
                         {"path": "c", "status": "D", "added": 0, "deleted": 7}]}
    assert js(["files.js"], "changesSummary(args)", changes) == "Changes since the base: 3 files, +3 −8"
    assert js(["files.js"], "changesSummary(args)", {"files": [{"path": "a", "status": "M", "added": 1, "deleted": 0}]}) == \
        "Changes since the base: 1 file, +1 −0"


def test_a_deep_link_to_a_hidden_task_resets_the_filter():
    tasks = PLAN["tasks"]
    pick = "filterForTask(args[0], args[1], args[2])"
    assert js(["plan.js"], pick, [tasks, "stopped", "task-1"]) == "all"
    assert js(["plan.js"], pick, [tasks, "stopped", "task-2"]) == "stopped"
    assert js(["plan.js"], pick, [tasks, "stopped", "tasks"]) == "stopped"
    assert js(["plan.js"], pick, [tasks, "stopped", "task-99"]) == "stopped"


def test_the_truncation_note_counts_bytes_rounded_up():
    kb = "shownKb(args)"
    assert js(["artifact.js", "files.js"], kb, "a" * 1025) == 2
    assert js(["artifact.js", "files.js"], kb, "é" * 1024) == 2   # 2048 bytes, 1024 chars
    assert js(["artifact.js", "files.js"], kb, "") == 1


def test_only_a_run_without_a_branch_reads_no_branch_yet():
    none = "changesNone(args)"
    assert js(["files.js"], none, {"facts": {"branch": None}}) is True
    assert js(["files.js"], none, {"facts": {"branch": "flow/x"}}) is False
    assert js(["files.js"], none, None) is True  # no detail to say otherwise: the old answer


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


# A fake `api` whose answers the test hands out one by one: each call waits on its own deferred promise.
READS = """
var ui = {run: null, sessions: null};
var toast = () => {};
var runPath = (id) => "/api/runs/" + id;
var calls = [];
var api = (path) => new Promise((resolve) => calls.push({path: path, resolve: resolve}));
const tick = async () => { for (let i = 0; i < 20; i++) await null; };  // the vm has no timers: let the reads settle
const page = (texts, next) => ({turns: texts.map((t) => ({kind: "text", text: t})), next: next, size: 900, done: false});
const shown = (st) => ({open: st.open, loaded: st.loaded, next: st.next, turns: st.turns.map((t) => t.text)});
"""


def test_a_session_opened_while_another_is_read_is_read_too():
    got = js_async(["sessions.js"], READS + """(async () => {
        const st = sessionsState("r1");
        openTranscript(st, "s1");
        openTranscript(st, "s2");  // s1's read is still out
        const paths = calls.map((c) => c.path);
        calls[0].resolve(page(["from s1"], 500));
        await tick();
        const meanwhile = shown(st);
        calls[calls.length - 1].resolve(page(["from s2"], 300));
        await tick();
        return {paths: paths, meanwhile: meanwhile, after: shown(st)};
    })()""", None)
    assert got["paths"] == ["/api/runs/r1/sessions/s1/transcript?after=0", "/api/runs/r1/sessions/s2/transcript?after=0"]
    assert got["meanwhile"] == {"open": "s2", "loaded": False, "next": 0, "turns": []}  # s1's page is not s2's
    assert got["after"] == {"open": "s2", "loaded": True, "next": 300, "turns": ["from s2"]}


def test_a_reopened_session_drops_a_page_read_before_it_was_closed():
    got = js_async(["sessions.js"], READS + """(async () => {
        const st = sessionsState("r1");
        openTranscript(st, "s1");
        calls[0].resolve(page(["first"], 500));
        await tick();
        readTranscript(st, false);  // a follow read from 500, still out when the reader leaves
        openTranscript(st, null);
        openTranscript(st, "s1");  // read again from the start
        const paths = calls.map((c) => c.path);
        calls[1].resolve(page(["late"], 900));
        await tick();
        if (calls[2]) calls[2].resolve(page(["first"], 500));  // no third read: the reopened session is stranded
        await tick();
        return {paths: paths, after: shown(st)};
    })()""", None)
    assert got["paths"][1:] == ["/api/runs/r1/sessions/s1/transcript?after=500", "/api/runs/r1/sessions/s1/transcript?after=0"]
    assert got["after"] == {"open": "s1", "loaded": True, "next": 500, "turns": ["first"]}


def test_load_more_is_not_offered_when_a_read_finds_no_complete_line():
    got = js_async(["sessions.js"], READS + """(async () => {
        const st = sessionsState("r1");
        openTranscript(st, "s1");
        calls[0].resolve(page(["a"], 500));
        await tick();
        const first = st.stalled;
        readTranscript(st, false);  // the last line has no newline yet: nothing comes back and `next` stays
        calls[1].resolve({turns: [], next: 500, size: 900, done: false});
        await tick();
        const stuck = st.stalled;
        readTranscript(st, false);
        calls[2].resolve(page(["b"], 900));
        await tick();
        return {first: first, stuck: stuck, moved: st.stalled};
    })()""", None)
    assert got == {"first": False, "stuck": True, "moved": False}


def test_agents_group_by_run_with_idle_rows_first():
    rows = [{"run_id": "r1", "module": "a", "idle": False, "age_s": 50}, {"run_id": "r2", "module": "b", "idle": False, "age_s": 9},
            {"run_id": "r1", "module": "a", "idle": True, "age_s": 10}]
    got = js(["agents.js"], "agentsByRun(args).map(g => [g.run_id, g.rows.map(r => r.age_s)])", rows)
    assert got == [["r1", [10, 50]], ["r2", [9]]]


def test_a_repeated_block_cannot_be_approved_without_a_note():   # spec Verification U4, Review Focus 1
    item = {"run_id": "r", "kind": "blocked_repeat", "key": "build/x", "approve_note": "required"}
    refused = js(["inbox.js"], "approvePayload(args, {note: '   '})", item)
    assert refused["ok"] is False and "blocked again" in refused["error"]
    assert js(["inbox.js"], "approvePayload(args, {note: 'fixed the fixture'})", item) == \
        {"ok": True, "body": {"target": "build/x", "note": "fixed the fixture"}}


def test_a_phase_approval_never_sends_a_note():
    item = {"run_id": "r", "kind": "phase_approval", "phase": "define", "key": "define_approved", "approve_note": "none"}
    assert js(["inbox.js"], "approvePayload(args, {note: 'x'})", item) == {"ok": True, "body": {"target": "define"}}


def test_an_optional_note_is_sent_only_when_written():
    item = {"run_id": "r", "kind": "blocked", "key": "build/x", "approve_note": "optional"}
    assert js(["inbox.js"], "approvePayload(args, {note: ' '})", item) == {"ok": True, "body": {"target": "build/x"}}
    assert js(["inbox.js"], "approvePayload(args, {note: ' go '})", item) == \
        {"ok": True, "body": {"target": "build/x", "note": "go"}}


def test_a_phase_approval_carries_what_the_page_showed():
    item = {"run_id": "r", "kind": "phase_approval", "phase": "plan", "key": "plan_approved", "approve_note": "none"}
    got = js(["inbox.js"], "(viewedList = (id) => [{path: 'p.md', sha: 'abcdef1', run: id}], approvePayload(args, {note: ''}))", item)
    assert got == {"ok": True, "body": {"target": "plan", "viewed": [{"path": "p.md", "sha": "abcdef1", "run": "r"}]}}


def test_questions_start_on_their_recommended_option():           # spec Verification U4
    qs = [{"id": "q1", "options": [{"key": "A"}, {"key": "B", "recommended": True}]}, {"id": "q2", "options": []}]
    assert js(["inbox.js"], "answerDefaults(args)", qs) == {"q1": "B", "q2": ""}


def test_an_answer_posts_its_own_key_and_prefers_written_text():  # Review Focus 2
    item = {"key": "build/build/task-4/implement"}
    got = js(["inbox.js"], "answerPayload(args, {q1: {choice: 'B', other: ''}, q2: {choice: '', other: ' pg '}})", item)
    assert got == {"ok": True, "body": {"key": "build/build/task-4/implement", "answers": {"q1": "B", "q2": "pg"}}}
    empty = js(["inbox.js"], "answerPayload(args, {q1: {choice: '', other: ' '}})", item)
    assert empty == {"ok": False, "error": "Choose or write at least one answer."}


def test_a_rejection_needs_a_note():
    item = {"kind": "phase_approval", "phase": "define", "key": "define_approved"}
    assert js(["inbox.js"], "rejectPayload(args, ' ')", item) == {"ok": False, "error": "A rejection needs a note."}
    assert js(["inbox.js"], "rejectPayload(args, 'cut scope')", item) == {"ok": True, "body": {"target": "define", "note": "cut scope"}}
    stop = {"kind": "blocked", "key": "build/x"}
    assert js(["inbox.js"], "rejectPayload(args, 'no')", stop)["body"]["target"] == "build/x"


def test_a_partial_answer_keeps_its_draft_and_others_forget_it():
    # `/answer` succeeds with complete: false when a question is still blank; the draft must keep the picks
    assert js(["inbox.js"], "inboxForgets('answer')", None) is False
    assert js(["inbox.js"], "['approve', 'reject'].map(inboxForgets)", None) == [True, True]


def test_items_group_by_run_or_by_kind():
    items = [{"run_id": "r1", "run_label": "a", "group": "stop", "key": "1"},
             {"run_id": "r2", "run_label": "b", "group": "approval", "key": "2"},
             {"run_id": "r1", "run_label": "a", "group": "question", "key": "3"}]
    by_run = js(["inbox.js"], "groupItems(args, 'run').map(g => [g.label, g.items.map(i => i.key)])", items)
    by_kind = js(["inbox.js"], "groupItems(args, 'kind').map(g => [g.label, g.items.map(i => i.key)])", items)
    assert by_run == [["a", ["1", "3"]], ["b", ["2"]]]
    assert by_kind == [["Approvals", ["2"]], ["Questions", ["3"]], ["Stops", ["1"]]]
    assert js(["inbox.js"], "groupItems(args, 'kind').map(g => g.key)", items[:1]) == ["stop"]
    assert js(["inbox.js"], "groupItems(args, 'run')", []) == []


def test_a_card_decided_elsewhere_says_by_whom():                  # spec Verification U4, Review Focus 3
    item = {"run_id": "r", "kind": "phase_approval", "phase": "define", "key": "define_approved"}
    recent = [{"run_id": "r", "phase": "define", "key": None, "action": "approved", "by": "alex",
               "ts": "2026-11-16T14:02:10+00:00"}]
    text = js(["inbox.js"], "goneText(args.item, args.recent)", {"item": item, "recent": recent})
    assert text.startswith("Approved from the CLI by alex at ")
    assert js(["inbox.js"], "goneText(args, [])", item) == "No longer waiting on you"


def test_the_gone_text_names_where_and_picks_the_newest_matching_entry():
    stop = {"run_id": "r", "kind": "blocked", "key": "build/build/task-1/implement"}
    other = {"run_id": "r2", "key": "build/build/task-1/implement", "action": "answered", "by": "zed", "ts": "2026-11-16T15:00:00+00:00"}
    mine = lambda action, by, ts: {"run_id": "r", "key": stop["key"], "phase": "build", "action": action, "by": by, "ts": ts}  # noqa: E731
    pick = "goneText(args.item, args.recent)"
    got = js(["inbox.js"], pick, {"item": stop, "recent": [other, mine("approved", "dashboard", "2026-11-16T14:00:00+00:00")]})
    assert got.startswith("Approved from the dashboard at ") and len(got.split(" at ")[1]) == 5
    got = js(["inbox.js"], pick, {"item": stop, "recent": [mine("answered", "runner", "2026-11-16T14:00:00+00:00")]})
    assert got.startswith("Answered by the runner at ")
    got = js(["inbox.js"], pick, {"item": stop, "recent": [mine("rejected", "sam", "2026-11-16T14:00:00+00:00"),
                                                           mine("approved", "alex", "2026-11-16T14:05:00+00:00")]})
    assert got.startswith("Approved from the CLI by alex at ")  # the newest entry of the two
    assert js(["inbox.js"], pick, {"item": stop, "recent": [other]}) == "No longer waiting on you"  # another run's key
    approval = {"run_id": "r", "kind": "phase_approval", "phase": "build", "key": "build_approved"}
    assert js(["inbox.js"], pick, {"item": approval, "recent": [mine("approved", "sam", "2026-11-16T14:00:00+00:00")]}) == \
        "No longer waiting on you"  # a phase approval matches its phase's entry, not a step's key inside that phase
    phase_entry = {"run_id": "r", "key": "build", "phase": "build", "action": "rejected", "by": "sam", "ts": "2026-11-16T14:00:00+00:00"}
    assert js(["inbox.js"], pick, {"item": approval, "recent": [phase_entry]}).startswith("Rejected from the CLI by sam at ")

