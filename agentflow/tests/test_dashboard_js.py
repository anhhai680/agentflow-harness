"""The dashboard's plan task board: its pure helpers, run with node and no DOM."""
from __future__ import annotations

from pathlib import Path

import pytest
from conftest import NODE, js, js_async, walk

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


def approval(run, **kw):
    return {"run_id": run, "kind": "phase_approval", "group": "approval", "phase": "define", "key": "define_approved",
            "actions": ["approve", "reject"], "approve_note": "none", **kw}


def test_batch_approvals_keeps_the_approvals_that_take_no_note():
    items = [approval("r1"), approval("r2", phase="plan"),
             {"run_id": "r1", "kind": "blocked", "group": "stop", "key": "build/x", "actions": ["approve", "reject"],
              "approve_note": "optional"},
             approval("r3", approve_note="required"),
             approval("r4", actions=["reject"]),
             {"run_id": "r5", "kind": "parked", "group": "stop", "key": "build/y", "actions": ["approve"],
              "approve_note": "optional"}]
    assert js(["inbox.js"], "batchApprovals(args).map(i => i.run_id)", items) == ["r1", "r2"]


def test_approve_all_sends_only_the_approvals_shown_when_it_armed():
    """An approval that arrives between the arming press and the confirming one is not approved unseen."""
    got = js_async(["inbox.js"], """(async () => {
      var sent = [];
      setTimeout = () => 0; clearTimeout = () => {};
      renderInbox = () => {};
      act = async (run, action, body) => { sent.push(run + ":" + action); return true; };
      viewedList = () => [];
      ui = {inbox: {view: {items: args.slice(0, 2)}, armed: null, armedAt: 0, drafts: new Map(), busy: new Set(),
                    sent: new Set()}};
      approveAllItems();  // arms over r1 and r2
      ui.inbox.view = {items: args};  // r3 arrives in the 4 seconds
      approveAllItems();  // confirms
      for (let i = 0; i < 50; i++) await null;
      return sent;
    })()""", [approval("r1"), approval("r2", phase="plan"), approval("r3")])
    assert got == ["r1:approve", "r2:approve"]


def test_a_constraint_approval_sends_the_flag_and_still_needs_a_required_note():
    item = {"run_id": "r", "kind": "gate_failed", "key": "build/build/task-2/commit", "approve_note": "optional",
            "actions": ["approve", "reject", "approve_constraint"]}
    assert js(["inbox.js"], "constraintPayload(args, {note: ' '})", item) == \
        {"ok": True, "body": {"target": "build/build/task-2/commit", "constraint": True}}
    again = dict(item, approve_note="required")
    assert js(["inbox.js"], "constraintPayload(args, {note: ''})", again)["ok"] is False


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


def test_an_answer_filled_into_questions_md_is_named_as_such():
    item = {"run_id": "r", "kind": "parked", "key": "build/x"}
    recent = [{"run_id": "r", "phase": "build", "key": "build/x", "action": "approved", "by": "questions.md",
               "ts": "2026-11-16T14:02:10+00:00"}]
    text = js(["inbox.js"], "goneText(args.item, args.recent)", {"item": item, "recent": recent})
    assert text.startswith("Answered in questions.md at ") and "CLI" not in text


def test_a_key_that_comes_back_as_a_new_item_drops_the_old_draft():
    pair = {"kept": {"since": "2026-11-16T10:00:00+00:00"}, "live": {"since": "2026-11-16T11:00:00+00:00"}}
    assert js(["inbox.js"], "inboxReturned(args.kept, args.live)", pair) is True
    same = {"kept": pair["kept"], "live": dict(pair["kept"])}
    assert js(["inbox.js"], "inboxReturned(args.kept, args.live)", same) is False


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



def test_the_activity_query_reads_its_state():
    q = lambda s: js(["activity.js"], "activityQuery(args)", s)  # noqa: E731
    assert q({"filter": "all", "before": None, "turns": False}) == "?limit=200"
    assert q({"filter": "decisions", "before": 812, "turns": True}) == "?limit=200&kinds=decisions&before=812&turns=1"


def test_pages_merge_by_seq_newest_first_without_duplicates():
    have = [{"seq": 9, "headline": "old"}, {"seq": 5}]
    got = js(["activity.js"], "mergePages(args.have, args.page).map(e => [e.seq, e.headline || ''])",
             {"have": have, "page": [{"seq": 12}, {"seq": 9, "headline": "new"}]})
    assert got == [[12, ""], [9, "new"], [5, ""]]


CARDS = [{"run_id": "20261116-0900-feature-crm", "lane": "feature", "status": "running", "module": "crm", "issue": "81",
          "branch": "flow/feature/crm", "waiting": None},
         {"run_id": "20261116-0800-bug-auth", "lane": "bug", "status": "waiting_human", "module": "auth[v2]", "issue": None,
          "branch": "flow/bug/auth", "waiting": {"kind": "blocked"}}]
VIEWS_STUB = str(Path(__file__).parent / "fixtures" / "js" / "_views_stub.js")


@pytest.mark.parametrize("query, expected", [
    ({}, ["crm", "auth[v2]"]),
    ({"lane": "bug"}, ["auth[v2]"]),
    ({"needs": True}, ["auth[v2]"]),
    ({"q": "#81"}, ["crm"]), ({"q": "81"}, ["crm"]),
    ({"q": "[v2"}, ["auth[v2]"]), ({"q": "(*"}, []), ({"q": "<b>"}, []),            # a search is plain text
    ({"status": "nope"}, ["crm", "auth[v2]"]), ({"lane": "nope"}, ["crm", "auth[v2]"]),  # an unknown value filters nothing
])
def test_cards_filter_by_lane_status_needs_and_plain_text(query, expected):
    got = js(["board.js"], "filterCards(args.cards, args.query, ['feature', 'bug'], ['running', 'waiting_human']).map(c => c.module)",
             {"cards": CARDS, "query": query})
    assert got == expected


def test_the_board_query_round_trips_through_the_hash():
    parse = lambda h: js([VIEWS_STUB, "run.js"], "parseRoute(args).query", h)  # noqa: E731
    assert parse("#/board?status=nope&x=1&needs=1&q=a%20b") == {"lane": None, "status": "nope", "needs": True, "q": "a b"}
    assert parse("#/board?needs=yes")["needs"] is False and parse("#/board") == {"lane": None, "status": None, "needs": False, "q": None}
    assert js([VIEWS_STUB, "run.js"], "parseRoute(args).view", "#/board?lane=bug") == "board"
    assert js([VIEWS_STUB, "run.js"], "routeHash({view: 'board', query: {lane: 'bug', needs: true, q: ''}})", None) == "#/board?lane=bug&needs=1"
    assert js([VIEWS_STUB, "run.js"], "routeHash({view: 'board', query: {lane: 'bug', status: 'running', needs: true, q: 'a b&c'}})", None) == \
        "#/board?lane=bug&status=running&needs=1&q=a+b%26c"
    assert js([VIEWS_STUB, "run.js"], "routeHash({view: 'board', query: {}})", None) == "#/board"
    assert js([VIEWS_STUB, "run.js"], "routeHash({view: 'board'})", None) == "#/board"
    assert js([VIEWS_STUB, "run.js"], "routeHash({view: 'agents', query: {lane: 'bug'}})", None) == "#/agents"
    assert js([VIEWS_STUB, "run.js"], "parseRoute(args).view", "#/run/r1/gates") == "run"


def test_parked_labels_name_each_parked_step():
    card = {"parked": [{"key": "build/build/task-3/implement", "label": "Task 3 · implement", "kind": "needs_human",
                        "summary": "needs a decision", "since": "2026-11-22T00:00:00+00:00"},
                       {"key": "build/build/task-2/implement", "label": "", "kind": "blocked", "summary": "", "since": None}]}
    assert js(["board.js"], "parkedLabels(args.card)", {"card": card}) == [
        {"text": "parked: Task 3 · implement", "title": "needs a decision"},
        {"text": "parked: build/build/task-2/implement", "title": ""}]
    assert js(["board.js"], "parkedLabels(args.card)", {"card": {"parked": []}}) == []
    assert js(["board.js"], "parkedLabels(args.card)", {"card": {}}) == []


def test_done_shows_the_newest_first_up_to_the_cap():
    done = [{"run_id": f"r{i}", "ended_at": f"2026-11-{10 + i:02d}T00:00:00+00:00"} for i in range(5)]
    got = js(["board.js"], "doneCards(args, 3, false)", done)
    assert [c["run_id"] for c in got["cards"]] == ["r4", "r3", "r2"] and got["hidden"] == 2
    assert js(["board.js"], "doneCards(args, 3, true).hidden", done) == 0
    tied = [{"run_id": "a", "ended_at": None}, {"run_id": "b", "ended_at": None}, {"run_id": "c", "ended_at": "2026-11-10T00:00:00+00:00"}]
    assert [c["run_id"] for c in js(["board.js"], "doneCards(args, 9, false).cards", tied)] == ["c", "b", "a"]


def test_the_token_table_reads_one_row_per_step_kind():
    rows = js(["tokens.js"], "tokenRows(args)", [{"kind": "build.implement", "sessions": 101, "cost_usd": 48.2,
              "usd": {"cache_write": 23.73, "cache_read": 12.4, "output": 11.98, "input": 0},
              "turns_p50": 16, "turns_p90": 37, "prefix_write_p50": 25404, "tool_output_chars": 5950000}])
    assert rows == [["build.implement", "101", "$48.20", "$23.73", "$12.40", "$11.98", "16 · 37", "25.4K", "6.0M chars"]]


def test_compact_and_an_empty_table():
    assert js(["tokens.js"], "[compact(999), compact(1000), compact(2500000)]", None) == ["999", "1.0K", "2.5M"]
    assert js(["tokens.js"], "tokenRows(args)", []) == []


DOM_STUB = str(Path(__file__).parent / "fixtures" / "js" / "_dom_stub.js")


def test_a_number_map_sends_numbers_and_skips_an_empty_value():
    """`session_budget_usd` is a `number` map: the Settings page must send numbers, which the server's check requires."""
    values = js([DOM_STUB, "settings.js"], "args.map(v => mapValue({value: v}, 'number'))", ["7.5", "10", "0", "", "  ", "abc"])
    assert values == [7.5, 10, 0, None, None, "abc"]  # `undefined` (an empty value: no entry) comes back as null; text stays text
    assert js([DOM_STUB, "settings.js"], "mapValue({value: '3'}, 'int')", None) == 3
    assert js([DOM_STUB, "settings.js"], "mapValue({value: ''}, 'int')", None) is None  # an `int` map still sends null for an empty value
    assert js([DOM_STUB, "settings.js"], "mapValue({value: '7'}, 'text')", None) == "7"
    assert js([DOM_STUB, "settings.js"], "args.map(v => mapValue({value: v}, 'number') === undefined)", ["", "5"]) == [True, False]


def test_the_usage_pill_reads_short():
    usage = {"mode": "on", "level": "soft", "waiting": 2, "note": None, "error": None,
             "windows": {"five_hour": {"utilization": 0.82, "resets_at": 1800000000, "stale": False},
                         "seven_day": {"utilization": 0.41, "resets_at": 1800300000, "stale": False}}}
    label = js(["board.js"], "usageLabel(args)", usage)
    assert label["text"] == "5h 82% · 7d 41%" and label["level"] == "soft" and label["title"].endswith(" · 2 runs wait")
    assert label["title"].startswith("resets ") and label["note"] == ""
    assert js(["board.js"], "usageLabel(args).text", {**usage, "windows": {}}) == ""
    assert js(["board.js"], "usageLabel(args).note", {**usage, "error": "bad json"}) == \
        "usage window file unreadable: bad json"
    assert js(["board.js"], "usageLabel(args).note", {**usage, "note": "rose", "error": "x"}) == "rose"


def test_the_usage_pill_leaves_out_stale_windows_and_names_the_mode():
    stale = {"utilization": 0.9, "resets_at": 1800000000, "stale": True}
    fresh = {"utilization": 0.3, "resets_at": 1800000000, "stale": False}
    usage = {"mode": "observe", "level": "clear", "waiting": 1, "note": None, "error": None,
             "windows": {"five_hour": stale, "seven_day": fresh}}
    label = js(["board.js"], "usageLabel(args)", usage)
    assert label["text"] == "7d 30% (observe)" and label["title"].endswith(" · 1 run waits")
    assert js(["board.js"], "usageLabel(args).text", {**usage, "mode": "off"}) == "7d 30% (off)"
    assert js(["board.js"], "usageLabel(args).title", {**usage, "waiting": 0}).count("·") == 0


def test_severity_class_knows_the_four_and_nothing_else():
    got = js(["board.js"], "['low','medium','high','critical','urgent','<b>',null].map(severityClass)", {})
    assert got == ["sev-low", "sev-medium", "sev-high", "sev-critical", None, None, None]


def test_alert_lines_count_and_name_dependabot_prs():
    item = {"alerts": [42, 43], "manifest": "requirements.txt", "dependabot_prs": [7]}
    assert js(["board.js"], "alertLines(args)", item) == ["2 alerts · requirements.txt", "Dependabot PR #7 open"]
    assert js(["board.js"], "alertLines(args)", {**item, "alerts": [42], "dependabot_prs": []}) == [
        "1 alert · requirements.txt"]


# `board.js` loads with no DOM, so these tests swap `el` for one that records its props and children as plain data.
FAKE_EL = ("el = (tag, p, ...c) => ({tag, p: p || {}, c: c.flat().filter((x) => x), addEventListener() {}});"
           "globalThis.Node = Object;")


def test_backlog_mark_names_the_alert_statuses():
    assert js(["board.js"], FAKE_EL + "backlogMark({status: 'no_fix'}).p", {}) == {"class": "badge lock", "text": "No fix yet"}
    assert js(["board.js"], FAKE_EL + "backlogMark({status: 'in_progress'}).p", {}) == {"class": "badge", "text": "In progress"}


ALERT = {"kind": "alert", "key": "alert:42", "label": "<b>requests</b> (pip)", "status": "ready", "severity": "high",
         "manifest": "requirements.txt", "alerts": [42], "dependabot_prs": [7], "blocked_by": [], "blocks": [],
         "parallel_with": [], "note": "", "failure": "", "removable": False,
         "url": "https://github.com/acme/app/security/dependabot/42"}


def test_an_alert_card_draws_its_pill_lines_and_link_as_text():
    node = js(["board.js"], FAKE_EL + "backlogNode(args)", ALERT)
    kids = node["c"]
    assert kids[0]["p"] == {"class": "title", "text": "<b>requests</b> (pip)"}
    assert kids[1]["p"] == {"class": "pill sev-high", "text": "high"}
    assert [k["p"] for k in kids[3:5]] == [{"class": "sub", "text": "1 alert · requirements.txt"},
                                           {"class": "sub", "text": "Dependabot PR #7 open"}]
    link = kids[5]["c"][0]
    assert link["p"]["text"] == "Alert ↗" and link["p"]["href"] == ALERT["url"]
    assert all("innerHTML" not in k["p"] for k in kids)


def test_an_alert_card_with_an_unknown_severity_draws_no_pill_and_an_issue_keeps_its_link_text():
    node = js(["board.js"], FAKE_EL + "backlogNode(args)", {**ALERT, "severity": "<b>", "url": ""})
    assert not [k for k in node["c"] if "pill" in (k["p"].get("class") or "")]
    assert not [k for k in node["c"] if k["tag"] == "div" and k["c"] and k["c"][0]["tag"] == "a"]  # no url: no link
    issue = {**ALERT, "kind": "issue", "key": "issue:1", "label": "#1 · x"}
    link = js(["board.js"], FAKE_EL + "backlogNode(args)", issue)["c"][2]["c"][0]
    assert link["p"]["text"] == "Issue ↗"


IDEA = {**ALERT, "kind": "module", "key": "module:i", "id": "i", "label": "An idea", "title": "An idea", "status": "ready",
        "planning": "backlog", "startable": True, "severity": None, "alerts": [], "dependabot_prs": [], "url": ""}


def _chips(item: dict) -> list:
    return [k["p"].get("text") for k in js(["board.js"], FAKE_EL + "backlogNode(args)", item)["c"]]


def test_an_idea_card_has_an_idea_chip_and_a_ready_one_does_not():
    assert "idea" in _chips(IDEA) and "idea" not in _chips({**IDEA, "planning": "ready"})
    assert "Start" in [b["p"].get("text") for k in js(["board.js"], FAKE_EL + "backlogNode(args)", IDEA)["c"]
                       for b in k["c"] if k["p"].get("class") == "moves"]


def test_an_item_that_is_not_startable_has_a_disabled_start_with_the_note():
    note = "runs in bug: only dashboard.backlog.module_lane items start from the Board"
    node = js(["board.js"], FAKE_EL + "backlogNode(args)", {**IDEA, "planning": "ready", "startable": False, "note": note})
    start = next(b for k in node["c"] if k["p"].get("class") == "moves" for b in k["c"])
    assert start["p"]["text"] == "Start" and start["p"]["disabled"] == "disabled" and start["p"]["title"] == note


def test_an_armed_idea_card_says_confirm_start():
    node = js(["board.js"], FAKE_EL + "backlogNode(args)", {**IDEA, "armed": True})
    assert [b["p"]["text"] for k in node["c"] if k["p"].get("class") == "moves" for b in k["c"]] == ["Confirm start"]


ARM_STUBS = ("ui = {board: null, backlogArm: null, backlogArmAt: 0}; var sent = [], timers = [], now = 1000;"
             "setTimeout = (fn, ms) => { timers.push(ms); return 1; }; clearTimeout = () => {}; Date.now = () => now;"
             "toast = () => {}; refreshAfter = () => {}; api = async (url, body) => { sent.push([url, body]); return {}; };")


def test_an_idea_cards_start_arms_on_the_first_click_and_confirms_on_the_second_within_four_seconds():
    got = js_async(["board.js"], ARM_STUBS + """(async () => {
      await startItem(args);  // arms
      const first = [sent.length, ui.backlogArm];
      now += 3999;
      await startItem(args);  // confirms
      return [first, sent, ui.backlogArm, timers];
    })()""", IDEA)
    assert got == [[0, "module:i"], [["/api/backlog/start", {"module": "i", "confirm": True}]], None, [4000]]


def test_an_idea_cards_arming_lapses_after_four_seconds():
    got = js_async(["board.js"], ARM_STUBS + """(async () => {
      await startItem(args);
      now += 4001;
      await startItem(args);  // too late: it arms again and sends nothing
      return [sent, ui.backlogArm];
    })()""", IDEA)
    assert got == [[], "module:i"]


def test_a_ready_item_starts_on_one_click_with_no_confirm():
    got = js_async(["board.js"], ARM_STUBS + "(async () => { await startItem(args); return sent; })()",
                   {**IDEA, "planning": "ready"})
    assert got == [["/api/backlog/start", {"module": "i"}]]


def test_the_skipped_list_is_headed_not_on_the_backlog_for_a_backlog_pack():
    stubs = FAKE_EL + "ui = {skippedOpen: false};"
    node = js(["board.js"], stubs + "skippedNode(args, 'backlog')", ["a.md", "<b>b</b>.md"])
    assert node["tag"] == "details" and node["c"][0]["p"]["text"] == "Not on the backlog (2)"
    assert [c["p"]["text"] for c in node["c"][1:]] == ["a.md", "<b>b</b>.md"]
    other = js(["board.js"], stubs + "skippedNode(args, 'specs')", ["a.md"])
    assert other["p"]["text"] == "Not modules: a.md"


def test_dropped_items_fold_under_a_counted_heading_and_draw_their_titles_as_text():
    node = js(["board.js"], FAKE_EL + "ui = {droppedOpen: false}; droppedNode(args)",
              [{"id": "z", "title": "<b>Gone</b>"}, {"id": "y", "title": ""}])
    assert node["tag"] == "details" and node["c"][0]["p"]["text"] == "Dropped (2)"
    assert [c["p"] for c in node["c"][1:]] == [{"class": "muted", "text": "z · <b>Gone</b>"}, {"class": "muted", "text": "y"}]
    assert js(["board.js"], FAKE_EL + "ui = {}; droppedNode(args)", []) is None


# ------------------------------------------------------------------ the backlog file on the Board

FILE_ITEM = {"title": "An idea", "lane": "", "status": "backlog", "depends_on": ["a", "b"], "issue": None, "spec": "docs/specs/i.md",
             "notes": "keep this"}
FILE_CARD = {**IDEA, "item": FILE_ITEM, "local": False}
SHA = "ab" * 32
NEW_SHA = "cd" * 32
BOARD = "ui = {board: {backlog_sha256: '" + NEW_SHA + "', modules: ['a', 'b', 'i']}};"  # a refresh has moved it since the card was drawn


def _buttons(node: dict) -> list:
    return [b["p"]["text"] for k in node["c"] if k["p"].get("class") == "moves" for b in k["c"]]


def test_edit_sends_only_the_changed_fields_with_the_sha():
    form = {"title": "An idea", "lane": "", "status": "ready", "depends_on": ["b", "a"], "issue": "12", "spec": "docs/specs/i.md",
            "notes": "keep this"}
    got = js(["board.js"], BOARD + "editBody(args.card, args.form, '" + SHA + "')", {"card": FILE_CARD, "form": form})
    assert got == {"op": "edit", "id": "i", "fields": {"status": "ready", "issue": 12}, "sha256": SHA}  # the dependencies are the same set
    form = {"title": " New ", "lane": "bug", "status": "backlog", "depends_on": ["b"], "issue": "", "spec": "", "notes": ""}
    got = js(["board.js"], BOARD + "editBody(args.card, args.form, '" + SHA + "')", {"card": FILE_CARD, "form": form})
    assert got["fields"] == {"title": "New", "lane": "bug", "depends_on": ["b"], "spec": "", "notes": ""}
    unchanged = {**FILE_ITEM}
    assert js(["board.js"], BOARD + "editBody(args.card, args.form, '" + SHA + "').fields", {"card": FILE_CARD, "form": unchanged}) == {}
    cleared = js(["board.js"], BOARD + "editBody(args.card, args.form, '" + SHA + "').fields",
                 {"card": {**FILE_CARD, "item": {**FILE_ITEM, "issue": 7}}, "form": {**FILE_ITEM, "issue": ""}})
    assert cleared == {"issue": None}


def test_move_and_add_bodies_carry_the_sha():
    assert js(["board.js"], BOARD + "moveBody(args, true, '" + SHA + "')", FILE_CARD) == {"op": "move", "id": "i", "direction": "up", "sha256": SHA}
    assert js(["board.js"], BOARD + "moveBody(args, false, '" + SHA + "')", FILE_CARD)["direction"] == "down"
    assert js(["board.js"], BOARD + "addFromSpecBody(args, '" + SHA + "')", "docs/specs/x.md") == {
        "op": "add", "item": {"spec": "docs/specs/x.md", "status": "backlog"}, "sha256": SHA}


def test_move_buttons_only_on_open_file_cards():
    on = _buttons(js(["board.js"], FAKE_EL + "backlogNode(args)", FILE_CARD))
    assert "Move up" in on and "Move down" in on and "Edit" in on
    form_card = {**IDEA, "kind": "card", "key": "card:c1", "removable": True}
    issue_card = {**IDEA, "kind": "issue", "key": "issue:7"}
    for card in (form_card, issue_card, IDEA):
        shown = _buttons(js(["board.js"], FAKE_EL + "backlogNode(args)", card))
        assert "Move up" not in shown and "Move down" not in shown and "Edit" not in shown
    done = {**FILE_CARD, "item": {**FILE_ITEM, "status": "done"}}
    assert "Move up" not in _buttons(js(["board.js"], FAKE_EL + "backlogNode(args)", done))


def test_a_local_card_has_a_local_badge():
    def badges(card):
        return [k["p"].get("text") for k in js(["board.js"], FAKE_EL + "backlogNode(args)", card)["c"]]
    assert "local: commit to share" in badges({**FILE_CARD, "local": True})
    assert "local: commit to share" not in badges(FILE_CARD)
    assert "local: commit to share" not in badges({**IDEA, "local": True})  # only a card read from the file has an item


def test_not_on_the_backlog_entries_have_an_add_button_that_sends_the_spec():
    stubs = FAKE_EL + "ui = {skippedOpen: false};"
    node = js(["board.js"], stubs + "skippedNode(args, 'backlog')", ["a.md", "<b>b</b>.md"])
    rows = node["c"][1:]
    assert [r["p"]["text"] for r in rows] == ["a.md", "<b>b</b>.md"]
    assert [[b["p"]["text"] for b in r["c"]] for r in rows] == [["Add"], ["Add"]]
    other = js(["board.js"], stubs + "skippedNode(args, 'specs')", ["a.md"])
    assert other["p"]["text"] == "Not modules: a.md" and not other.get("c")
    got = js_async(["board.js"], BOARD + "var sent = []; toast = () => {}; refreshAfter = () => {};"
                   "api = async (url, body) => { sent.push([url, body]); return {ok: true}; };"
                   "(async () => { await addSpec(args, '" + SHA + "'); return sent; })()", "docs/specs/x.md")
    assert got == [["/api/backlog/file", {"op": "add", "item": {"spec": "docs/specs/x.md", "status": "backlog"}, "sha256": SHA}]]


ISSUE_CARD = {**IDEA, "kind": "issue", "key": "issue:7", "id": "7", "label": "#7 · <b>hostile</b>", "addable": True, "sha": SHA}
CLICKABLE_EL = ("el = (tag, p, ...c) => ({tag, p: p || {}, c: c.flat().filter((x) => x), addEventListener() {}});"
                "globalThis.Node = Object;")


def _add_issue_run(card: dict) -> list:
    """Draw `card`, click its **Add to backlog** button once if it has one, and return the texts of its buttons and the posts."""
    return js_async(["board.js"], BOARD + CLICKABLE_EL + "var sent = []; toast = () => {}; refreshAfter = () => {};"
                    "api = async (url, body) => { sent.push([url, body]); return {ok: true}; };"
                    """(async () => {
      const node = backlogNode(args);
      const moves = node.c.filter((k) => k.p.class === "moves").flatMap((k) => k.c);
      const add = moves.find((b) => b.p.text === "Add to backlog");
      if (add) await add.p.onclick({ stopPropagation() {} });
      return [moves.map((b) => b.p.text), sent];
    })()""", card)


def test_an_addable_issue_card_posts_add_issue_once_with_its_number_and_sha():
    texts, sent = _add_issue_run(ISSUE_CARD)
    assert "Add to backlog" in texts
    assert sent == [["/api/backlog/file", {"op": "add_issue", "number": "7", "sha256": SHA}]]  # the render's sha, not NEW_SHA


def test_the_add_issue_button_is_fixed_text_and_only_on_an_addable_issue_card():
    texts, sent = _add_issue_run({**ISSUE_CARD, "addable": False})
    assert "Add to backlog" not in texts and sent == []
    texts, _ = _add_issue_run({k: v for k, v in ISSUE_CARD.items() if k != "addable"})
    assert "Add to backlog" not in texts
    texts, _ = _add_issue_run({**IDEA, "addable": True})  # only an issue card has it
    assert "Add to backlog" not in texts
    texts, _ = _add_issue_run({**ISSUE_CARD, "label": "Add to backlog"})
    assert texts.count("Add to backlog") == 1
    texts, _ = _add_issue_run({**ISSUE_CARD, "label": "<img src=x onerror=alert(1)>"})
    assert not any("<img" in t for t in texts)


NEW_DATA = {"work_source_kind": "backlog", "new_task": {"feature": ["issue", "new_module"], "bug": ["issue", "task"]},
            "modules": ["a", "b"], "lane_phases": {"feature": ["define", "plan"], "bug": ["debug"]}, "after_choices": [{"key": "x", "label": "x"}]}


def _ids(nodes) -> list:
    return [n["p"]["id"] for n in walk(nodes) if "id" in n.get("p", {})]


def test_the_new_task_form_in_a_backlog_pack_adds_to_the_file_and_hides_stop_and_after():
    parts = js(["board.js"], FAKE_EL + "taskFormParts(args, 'feature', {})", NEW_DATA)
    ids = _ids(parts)
    assert ids == ["nt-issue", "nt-module", "nt-resp", "nt-deps"]
    # other kinds of work and other packs keep today's form
    assert "nt-stop" in _ids(js(["board.js"], FAKE_EL + "taskFormParts(args, 'bug', {})", NEW_DATA))
    other = js(["board.js"], FAKE_EL + "taskFormParts(args, 'feature', {})", {**NEW_DATA, "work_source_kind": "specs"})
    assert "nt-stop" in _ids(other)
    body = js(["board.js"], "newModuleBody(args, '" + SHA + "')",
              {"module": " crm ", "responsibility": "Does the  CRM\n  thing", "deps": ["a"], "issue": "12"})
    assert body == {"op": "add", "item": {"id": "crm", "title": "Does the CRM thing", "depends_on": ["a"], "status": "ready", "issue": 12},
                    "sha256": SHA}
    no_issue = js(["board.js"], "newModuleBody(args, '" + SHA + "')", {"module": "m", "responsibility": "x" * 300, "deps": [], "issue": ""})
    assert "issue" not in no_issue["item"] and len(no_issue["item"]["title"]) == 200 and no_issue["item"]["title"].endswith("…")


STALE = "backlog.yml changed on disk since the page read it: reload"
WRITE_STUBS = ("var said = [], reloads = 0; toast = (t) => said.push(t); refreshAfter = () => { reloads++; };"
               "var fail = (status, message) => { api = async () => { const e = new Error(message); e.status = status; throw e; }; };")


def test_a_409_tells_the_person_to_reload():
    got = js_async(["board.js"], WRITE_STUBS + """(async () => {
      fail(409, "backlog.yml changed on disk since the page read it: reload");
      const first = await writeBacklogFile({op: "move"}, "ok");
      fail(409, "HTTP 409");
      const second = await writeBacklogFile({op: "move"}, "ok");
      fail(409, "crm has a live run, r1 (running); cancel it first or let it finish");
      await writeBacklogFile({op: "edit"}, "ok");
      fail(400, "already first");
      const third = await writeBacklogFile({op: "move"}, "ok");
      return [first, second, third, said, reloads];
    })()""", None)
    assert got == ["stale", "stale", "failed",
                   [STALE, STALE, "crm has a live run, r1 (running); cancel it first or let it finish", "Could not save: already first"], 4]


def test_a_saved_write_toasts_and_reloads():
    got = js_async(["board.js"], WRITE_STUBS + "api = async () => ({ok: true}); (async () => [await writeBacklogFile({}, 'Saved'), said, reloads])()", None)
    assert got == ["saved", ["Saved"], 1]


def test_the_run_card_shows_its_local_only_note_as_text():
    node = js(["board.js"], FAKE_EL + "ui = {}; money = limits = arrow = ago = dur = () => ''; goRun = openPeek = () => {}; cardNode(args)",
              {"run_id": "r", "module": "m", "status": "running", "badge": "b", "gates": [], "strip": [], "backlog_note": "<b>x</b> remove that entry",
               "agent": None, "agents": [], "time_in_phase_s": 1, "wall_s": 1})
    def texts(n):
        if isinstance(n, dict):
            yield n["p"].get("text")
            for child in n["c"]:
                yield from texts(child)
    notes = [t for t in texts(node) if t == "<b>x</b> remove that entry"]
    assert len(notes) == 1


# A write carries the sha of the render that drew what the person acted on, not the one a later refresh brought.
CLICK_STUBS = (BOARD + "var sent = []; toast = () => {}; refreshAfter = () => {}; dialog = () => {};"
               "api = async (url, body) => { sent.push(body); return {ok: true}; };")


def test_a_save_carries_the_sha_from_when_the_dialog_opened():
    form = {**FILE_ITEM, "status": "ready"}
    got = js_async(["board.js"], CLICK_STUBS + "(async () => { await saveEdit(args.card, args.form, '" + SHA + "'); return sent; })()",
                   {"card": FILE_CARD, "form": form})  # ui.board now holds NEW_SHA
    assert got == [{"op": "edit", "id": "i", "fields": {"status": "ready"}, "sha256": SHA}]
    nothing = js_async(["board.js"], CLICK_STUBS + "(async () => { await saveEdit(args.card, args.form, '" + SHA + "'); return sent; })()",
                       {"card": FILE_CARD, "form": FILE_ITEM})
    assert nothing == []


def _click(card: dict, text: str):
    return js_async(["board.js"], FAKE_EL + CLICK_STUBS + """(async () => {
      const node = backlogNode(args);
      const button = node.c.filter((k) => k.p.class === "moves").flatMap((k) => k.c).find((b) => b.p.text === '""" + text + """');
      await button.p.onclick({stopPropagation() {}});
      return sent;
    })()""", card)


def test_move_and_edit_buttons_use_the_sha_of_the_render_that_drew_the_card():
    assert _click({**FILE_CARD, "sha": SHA}, "Move up") == [{"op": "move", "id": "i", "direction": "up", "sha256": SHA}]
    assert _click({**FILE_CARD, "sha": SHA}, "Move down")[0]["sha256"] == SHA


def test_the_add_button_uses_the_sha_of_the_render_that_drew_the_row():
    got = js_async(["board.js"], FAKE_EL + CLICK_STUBS + "ui.skippedOpen = false;"
                   "(async () => { const row = skippedNode(args, 'backlog', '" + SHA + "').c[1]; await row.c[0].p.onclick(); return sent; })()", ["a.md"])
    assert got == [{"op": "add", "item": {"spec": "a.md", "status": "backlog"}, "sha256": SHA}]


def test_a_refresh_that_changes_the_file_redraws_the_backlog_cards():
    # the entry's data holds the sha, so `patchChildren` builds the card again instead of keeping one that carries the old sha
    source = Path(__file__).resolve().parent.parent.joinpath("dashboard_ui", "board.js").read_text(encoding="utf-8")
    assert source.count("sha: data.backlog_sha256") == 2


def test_the_edit_dialogs_lane_choices_are_the_packs_lanes():
    """Every lane of the pack, not only the New task form's, and the item's own lane even when the pack has dropped it."""
    stubs = ("el = (tag, p, ...c) => { const kids = c.flat().filter((x) => x);"
             " const n = {tag, p: p || {}, c: kids, addEventListener() {}, setAttribute(k, v) { n.p[k] = v; }};"
             " if (tag === 'select') n.options = kids; return n; }; globalThis.Node = Object;"
             "workLabel = (l) => l; var body; dialog = (title, b) => { body = b; };"
             "ui = {board: {backlog_sha256: 'x', modules: ['a', 'i'], lanes: ['feature', 'bug', 'vuln'],"
             " lane_phases: {feature: ['define']}}};")
    got = js(["board.js"], stubs + """editDialog(args);
      body.find((n) => n.p.id === 'ei-lane').c.map((o) => o.p.value)""", {**FILE_CARD, "item": {**FILE_ITEM, "lane": "old"}})
    assert got == ["", "feature", "bug", "vuln", "old"]


# ---- another machine's claim: a read-only card in its phase column

CLAIM = {"key": "claim:crm", "id": "crm", "title": "CRM sync", "lane": "feature", "column": "build", "phase": "build",
         "status": "running", "by": "Dana", "run": "20261125-120000-crm", "updated": "2026-11-22T12:00:00+00:00",
         "age_s": 3 * 86400 + 600, "stale": False, "unreadable": ""}


def _texts(node: dict) -> list:
    return [n["p"].get("text") for n in walk([node]) if n["p"].get("text") is not None]


def test_a_remote_claim_card_is_text_only():
    markup = "<img src=x onerror=1>"
    node = js(["board.js"], FAKE_EL + "remoteClaimNode(args)",
              {**CLAIM, "by": markup, "run": "<b>r</b>", "title": "<script>x</script>"})
    assert node["tag"] == "article" and "draggable" not in node["p"]
    assert not [n for n in walk([node]) if n["tag"] in ("button", "a", "input") or "onclick" in n["p"]]
    assert markup in " ".join(_texts(node))  # it is shown, as a text node, never parsed
    assert all(set(n["p"]) <= {"class", "text", "title", "role", "aria-label"} for n in walk([node]))
    # the run id reaches the page only as a tooltip, a text-valued property
    assert "<b>r</b>" in [n["p"].get("title") for n in walk([node])]


def test_the_card_names_the_claimant_phase_status_and_age():
    texts = " | ".join(_texts(js(["board.js"], FAKE_EL + "remoteClaimNode(args)", CLAIM)))
    for want in ("crm", "Dana", "build · running", "3 d"):
        assert want in texts
    assert "stale" not in texts
    ages = js(["board.js"], "args.map(claimAge)", [None, 30, 600, 7200, 86400 * 2 + 5])
    assert ages == ["", "just now", "10 min", "2 h", "2 d"]


def test_remote_cards_sit_in_their_phase_column():
    data = {"columns": ["Backlog", "plan", "build", "Done"], "backlog": [], "remote_claims": [
        CLAIM, {**CLAIM, "key": "claim:bad", "id": "bad", "column": "Backlog", "phase": "", "status": "",
                "by": "", "unreadable": "no claim.json", "age_s": None},
        {**CLAIM, "key": "claim:odd", "id": "odd", "column": "retired-phase"}]}
    stubs = FAKE_EL + "ui = {}; entry = (key, data, build) => ({key, data, build}); "

    def keys(column):
        return js(["board.js"], stubs + "columnEntries(args, " + repr(column) + ", []).map((e) => e.key)", data)

    assert keys("build") == ["col-head", "claim:crm"]
    assert keys("plan") == ["col-head"]
    # an unreadable claim, and one whose column the pack does not have, are in Backlog: never dropped
    assert keys("Backlog") == ["col-head", "claim:bad", "claim:odd"]
    head = js(["board.js"], stubs + "columnEntries(args, 'Backlog', [])[0].data", data)
    assert head["count"] == 2
    bad = js(["board.js"], stubs + "remoteClaimNode(args.remote_claims[1])", data)
    assert "no claim.json" in _texts(bad) and not [t for t in _texts(bad) if " · " in t]


def test_the_card_says_stale_and_the_note_shows():
    texts = _texts(js(["board.js"], FAKE_EL + "remoteClaimNode(args)", {**CLAIM, "stale": True}))
    assert "stale" in texts
    got = js(["board.js"], """
      var shown = {}; $ = (id) => (shown[id] = shown[id] || {hidden: false, textContent: ""});
      showClaimsNote(args); [shown["claims-note"].hidden, shown["claims-note"].textContent]""", "<b>origin</b> could not be read")
    assert got == [False, "<b>origin</b> could not be read"]
    assert js(["board.js"], """var shown = {}; $ = (id) => (shown[id] = shown[id] || {hidden: false, textContent: "x"});
      showClaimsNote(args); [shown["claims-note"].hidden, shown["claims-note"].textContent]""", None) == [True, ""]
    html = (Path(__file__).resolve().parent.parent / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="claims-note"' in html and html.index('id="backlog-note"') < html.index('id="claims-note"')


def test_a_saved_edit_shows_the_notes_the_server_sent_as_text():
    got = js_async(["board.js"], WRITE_STUBS + """api = async () => ({ok: true, notes: ["claim of crm freed", "<b>x</b>"]});
      (async () => { await writeBacklogFile({}, 'Saved'); return said; })()""", None)
    assert got == ["Saved. claim of crm freed. <b>x</b>"]


# ---- the repository switcher (W3): the path prefix, the route's repository, the selection, the chips and the title
PAGE_STUB = str(Path(__file__).parent / "fixtures" / "js" / "_page_stub.js")
REPOS = {"repos": [{"id": "api-1a2b3c4d", "name": "api", "state": "live", "waiting": 2, "active": 1, "failed": 0, "error": None},
                   {"id": "web-5e6f7a8b", "name": "web", "state": "live", "waiting": 0, "active": 0, "failed": 0, "error": None},
                   {"id": "docs-9c0d1e2f", "name": "docs", "state": "not_set_up", "waiting": 0, "active": 0, "failed": 0, "error": None},
                   {"id": "lib-0a1b2c3d", "name": "lib", "state": "error", "waiting": 0, "active": 0, "failed": 0, "error": "bad pack"}],
         "skipped": [], "default": "web-5e6f7a8b"}


def test_repo_path_prefixes_only_with_a_repository():
    assert js([PAGE_STUB, "core.js"], "repoPath('/api/board', 'api-1a2b3c4d')", None) == "/api/repos/api-1a2b3c4d/board"
    assert js([PAGE_STUB, "core.js"], "repoPath('/api/runs/r/approve', 'a/../b')", None) == "/api/repos/a%2F..%2Fb/runs/r/approve"
    assert js([PAGE_STUB, "core.js"], "[repoPath('/api/board', null), repoPath('/api/board', undefined)]", None) == ["/api/board"] * 2


def test_a_run_is_keyed_by_its_repository_on_a_page_of_several():
    assert js([PAGE_STUB, "core.js"], "[runKey('a', 'r1'), runKey(null, 'r1'), runKey(undefined, 'r1')]", None) == \
        ["a\u0000r1", "r1", "r1"]
    assert js([PAGE_STUB, "core.js"], "[ui.repo, ui.repos, ui.multi, ui.allRepos]", None) == [None, None, False, True]


def test_api_sends_to_the_selected_repository_unless_told_otherwise():
    got = js_async([PAGE_STUB, "core.js"], """var asked = [];
      fetch = async (path) => { asked.push(path); return {ok: true, status: 200, text: async () => "{}"}; };
      (async () => {
        await api('/api/board'); ui.repo = 'web-5e6f7a8b'; await api('/api/board'); await api('/api/repos', undefined, null);
        await api('/api/runs/r/approve', {}, 'api-1a2b3c4d'); return asked; })()""", None)
    assert got == ["/api/board", "/api/repos/web-5e6f7a8b/board", "/api/repos", "/api/repos/api-1a2b3c4d/runs/r/approve"]


def test_an_action_posts_to_its_own_repository_and_refreshes_only_its_own_run():
    got = js_async([PAGE_STUB, "core.js"], """var asked = [], after = [];
      fetch = async (path) => { asked.push(path); return {ok: true, status: 200, text: async () => "{}"}; };
      toast = () => {}; refreshAfter = (runId, repo) => after.push([runId, repo === undefined ? "-" : repo]);
      ui.repo = 'web-5e6f7a8b';
      (async () => { await act('r1', 'pause'); await act('r1', 'pause', {}, 'api-1a2b3c4d'); return [asked, after]; })()""", None)
    assert got == [["/api/repos/web-5e6f7a8b/runs/r1/pause", "/api/repos/api-1a2b3c4d/runs/r1/pause"],
                   [["r1", "-"], ["r1", "api-1a2b3c4d"]]]


def test_a_one_repository_view_shows_no_switcher():
    one = {"repos": REPOS["repos"][:1], "skipped": [], "default": "api-1a2b3c4d"}
    assert js(["repos.js"], "[isMulti(args.one), isMulti({repos: []}), isMulti(null), isMulti(args.all)]",
              {"one": one, "all": REPOS}) == [False, False, False, True]


def test_a_route_carries_its_repository_both_ways():
    parse = lambda h: js([VIEWS_STUB, "run.js"], "parseRoute(args)", h)  # noqa: E731
    back = lambda h: js([VIEWS_STUB, "run.js"], "routeHash(parseRoute(args))", h)  # noqa: E731
    run = parse("#/repo/api-1a2b3c4d/run/20261127-1200-x/plan")
    assert run["repo"] == "api-1a2b3c4d" and run["view"] == "run" and run["tab"] == "plan" and run["run"] == "20261127-1200-x"
    assert back("#/repo/api-1a2b3c4d/run/20261127-1200-x/plan") == "#/repo/api-1a2b3c4d/run/20261127-1200-x/plan"
    board = parse("#/repo/api-1a2b3c4d/board?lane=bug")
    assert board["repo"] == "api-1a2b3c4d" and board["view"] == "board" and board["query"]["lane"] == "bug"
    assert back("#/repo/api-1a2b3c4d/board?lane=bug") == "#/repo/api-1a2b3c4d/board?lane=bug"
    assert back("#/repo/a%2F..%2Fb/inbox") == "#/repo/a%2F..%2Fb/inbox" and parse("#/repo/a%2F..%2Fb/inbox")["repo"] == "a/../b"
    assert parse("#/board")["repo"] is None and parse("#/run/r1/gates")["repo"] is None
    assert js([VIEWS_STUB, "run.js"], "routeHash({view: 'inbox', repo: null})", None) == "#/inbox"
    for empty in ("#/repo/", "#/repo//board", "#/repo"):
        got = parse(empty)
        assert got["view"] == "board" and got["repo"] is None, empty
    assert parse("#/repo/%zz/board")["view"] == "board" and parse("#/repo/%zz/board")["repo"] is None  # a malformed %
    # a route with no `repo` key takes the page's selection; with no `ui` (this test) that is none
    assert js([VIEWS_STUB, "run.js"], "routeHash({view: 'run', run: 'r1', tab: 'gates'})", None) == "#/run/r1/gates"
    assert js([VIEWS_STUB, "run.js"], "ui = {repo: 'web-5e6f7a8b'}; routeHash({view: 'run', run: 'r1', tab: 'gates'})", None) == \
        "#/repo/web-5e6f7a8b/run/r1/gates"
    assert js([VIEWS_STUB, "run.js"], "ui = {repo: 'web-5e6f7a8b'}; routeHash({view: 'board', query: {lane: 'bug'}, repo: null})", None) == \
        "#/board?lane=bug"


def test_the_selection_takes_the_hash_then_the_session_then_the_default():
    pick = lambda h, s, view=REPOS: js(["repos.js"], "pickRepo(args.view, args.h, args.s)", {"view": view, "h": h, "s": s})  # noqa: E731
    assert pick("api-1a2b3c4d", "web-5e6f7a8b") == "api-1a2b3c4d"
    for bad in ("docs-9c0d1e2f", "lib-0a1b2c3d", "nope", "../../etc", "", None):
        assert pick(bad, "api-1a2b3c4d") == "api-1a2b3c4d", bad
    assert pick(None, "docs-9c0d1e2f") == "web-5e6f7a8b"
    assert pick(None, None, {**REPOS, "default": "docs-9c0d1e2f"}) == "api-1a2b3c4d"  # then the first live row
    assert pick(None, None, {**REPOS, "default": None}) == "api-1a2b3c4d"
    assert pick("api-1a2b3c4d", None, {"repos": REPOS["repos"][2:], "skipped": [], "default": None}) is None
    assert pick("api-1a2b3c4d", None, None) is None


def test_a_routed_repository_keeps_the_selection_unless_the_hash_names_a_live_one():
    route = lambda h, cur, s=None: js(["repos.js"], "routeRepo(args.view, args.h, args.cur, args.s)",  # noqa: E731
                                      {"view": REPOS, "h": h, "cur": cur, "s": s})
    assert route("api-1a2b3c4d", "web-5e6f7a8b") == "api-1a2b3c4d"
    assert route("nope", "web-5e6f7a8b") == "web-5e6f7a8b" and route(None, "web-5e6f7a8b") == "web-5e6f7a8b"
    assert route("nope", "lib-0a1b2c3d") == "lib-0a1b2c3d"  # the selected repository broke: the selection stays
    assert route(None, None, "api-1a2b3c4d") == "api-1a2b3c4d" and route(None, None, "docs-9c0d1e2f") == "web-5e6f7a8b"


def test_chips_put_not_set_up_last_and_disable_what_is_not_live():
    chips = js(["repos.js"], "chipModel(args, 'api-1a2b3c4d')", REPOS)
    assert [c["name"] for c in chips] == ["api", "web", "lib", "docs"]
    by = {c["name"]: c for c in chips}
    assert [by[n]["disabled"] for n in ("api", "web", "lib", "docs")] == [False, False, True, True]
    assert by["lib"]["note"] == "bad pack" and by["api"]["waiting"] == 2 and by["docs"]["state"] == "not_set_up"
    assert [c["selected"] for c in chips] == [True, False, False, False] and by["web"]["id"] == "web-5e6f7a8b"
    assert js(["repos.js"], "chipModel(null, null)", None) == []


def test_the_tab_title_names_the_repository_or_counts_them():
    assert js(["repos.js"], "tabTitle(args)", REPOS) == "agentflow · 4 repositories"
    assert js(["repos.js"], "tabTitle(args)", {"repos": REPOS["repos"][:1], "skipped": [], "default": None}) == "agentflow · api"
    assert js(["repos.js"], "[tabTitle({repos: []}), tabTitle(null)]", None) == ["agentflow", "agentflow"]


def test_a_message_of_another_repository_is_not_the_pages_own():
    assert js(["repos.js"], "[streamOwn({repo: 'a'}, true, 'b'), streamOwn({repo: 'a'}, true, 'a'), streamOwn({repo: 'a'}, false, null)]",
              None) == [False, True, True]
    assert js(["repos.js"], "[streamOwn({}, true, 'a'), streamOwn({}, false, null)]", None) == [False, True]


def test_waiting_total_sums_every_row():
    assert js(["repos.js"], "waitingTotal(args)", REPOS) == 2
    assert js(["repos.js"], "[waitingTotal(null), waitingTotal({repos: [{waiting: null}, {}]})]", None) == [0, 0]


def test_a_repository_name_comes_from_the_list_else_the_id():
    assert js(["repos.js"], "ui = {repos: args}; [repoName('lib-0a1b2c3d'), repoName('nope')]", REPOS) == ["lib", "nope"]
    assert js(["repos.js"], "ui = {repos: null}; repoName('x')", None) == "x"


def test_only_an_empty_list_or_no_selection_shows_no_repository():
    broken = {**REPOS["repos"][3]}
    cases = [({"repos": []}, False, None), ({"repos": [broken]}, False, None), ({"repos": REPOS["repos"][:1]}, False, None),
             (REPOS, True, None), (REPOS, True, "lib-0a1b2c3d"), (REPOS, True, "api-1a2b3c4d"), (None, False, None)]
    got = js(["repos.js"], "args.map(([view, multi, repo]) => nothingToShow(view, multi, repo))", cases)
    # a page of one whose repository is broken asks the legacy route and shows its reason, as before
    assert got == [True, False, False, True, False, False, False]


def test_leaving_a_repository_asks_before_dropping_unsaved_settings():
    stubs = ("ui = {repos: null, repo: 'a'}; var asked = []; settings = {edits: new Map([['k', 1]]), yamlDirty: false};"
             "confirm = (text) => { asked.push(text); return answer; };")
    no = js(["repos.js"], stubs + "var answer = false; [settingsMayLeave(), settings.edits.size, asked]", None)
    assert no == [False, 1, ["Discard the unsaved Settings changes in a?"]]
    yes = js(["repos.js"], stubs + "var answer = true; [settingsMayLeave(), settings.edits.size, settingsMayLeave(), asked.length]", None)
    assert yes == [True, 0, True, 1]  # once let go, the route that follows does not ask again
    clean = js(["repos.js"], stubs + "settings.edits.clear(); var answer = false; [settingsMayLeave(), asked.length]", None)
    assert clean == [True, 0]


def test_a_late_config_answer_of_another_load_is_dropped():
    got = js_async([DOM_STUB, "settings.js"], """var rendered = [], answers = {};
      api = (path) => new Promise((resolve) => { answers[Object.keys(answers).length] = resolve; });
      renderSettings = () => rendered.push(settings.data.name);
      (async () => {
        const first = loadSettings(); const second = loadSettings();
        answers[1]({name: 'new'}); await second; answers[0]({name: 'old'}); await first;
        const third = loadSettings(); settings.request++; answers[2]({name: 'switched away'}); await third;
        return [rendered, settings.data.name]; })()""", None)
    assert got == [["new"], "new"]


# ---- the All-repositories Inbox and Agents: each item acts on its own repository (Review Focus 1 and 5)
def test_items_of_two_repositories_with_one_run_id_have_two_keys():
    pair = {"a": {"repo": "a", "run_id": "r", "key": "k"}, "b": {"repo": "b", "run_id": "r", "key": "k"},
            "none": {"run_id": "r", "key": "k"}}
    got = js([PAGE_STUB, "core.js", "inbox.js"], "[inboxItemKey(args.a), inboxItemKey(args.b), inboxItemKey(args.none)]", pair)
    assert got[0] != got[1] and got[2] == "r\u0000k"
    assert js(["inbox.js"], "[inboxItemKey(args.a), inboxItemKey(args.none)]", pair) == [got[0], "r\u0000k"]  # no core.js: same keys


def test_items_group_by_repository_and_run():
    items = [{"repo": "a", "run_id": "r", "run_label": "x", "group": "stop", "key": "1"},
             {"repo": "b", "run_id": "r", "run_label": "x", "group": "stop", "key": "1"},
             {"repo": "a", "run_id": "r", "run_label": "x", "group": "question", "key": "2"}]
    got = js([PAGE_STUB, "core.js", "inbox.js"], "groupItems(args, 'run').map(g => [g.repo, g.items.map(i => i.key)])", items)
    assert got == [["a", ["1", "2"]], ["b", ["1"]]]
    keys = js([PAGE_STUB, "core.js", "inbox.js"], "groupItems(args, 'run').map(g => g.key)", items)
    assert keys[0] != keys[1]


def test_gone_text_reads_only_its_own_repositorys_recent():
    item = {"repo": "a", "run_id": "r", "kind": "blocked", "key": "build/x"}
    other = {"repo": "b", "run_id": "r", "key": "build/x", "phase": "build", "action": "approved", "by": "dashboard",
             "ts": "2026-11-16T14:00:00+00:00"}
    mine = {**other, "repo": "a"}
    pick = "goneText(args.item, args.recent)"
    assert js(["inbox.js"], pick, {"item": item, "recent": [other]}) == "No longer waiting on you"
    assert js(["inbox.js"], pick, {"item": item, "recent": [other, mine]}).startswith("Approved from the dashboard at ")
    untagged = {"run_id": "r", "kind": "blocked", "key": "build/x"}
    assert js(["inbox.js"], pick, {"item": untagged, "recent": [{**other, "repo": None}]}).startswith("Approved from the dashboard")


SEND_STUBS = """var recorded = []; toast = () => {}; refreshAfter = () => {};
  fetch = async (path) => { recorded.push(path); return {ok: true, status: 200, text: async () => '{"ok": true}'}; };"""


def test_an_inbox_action_posts_to_the_items_repository_not_the_selected_one():  # spec Testing strategy; Review Focus 1
    item = {"run_id": "20261127-1200-same", "key": "plan_approved", "kind": "phase_approval", "phase": "plan"}
    got = js_async([PAGE_STUB, "core.js", "inbox.js"], SEND_STUBS + """(async () => {
      ui.repo = "b"; ui.multi = true;
      await inboxSend({repo: "a", ...args}, "approve", {target: "plan"});
      return recorded; })()""", item)
    assert got == ["/api/repos/a/runs/20261127-1200-same/approve"]
    one = js_async([PAGE_STUB, "core.js", "inbox.js"], SEND_STUBS + """(async () => {
      ui.repo = null; ui.multi = false;
      await inboxSend(args, "approve", {target: "plan"});
      return recorded; })()""", item)
    assert one == ["/api/runs/20261127-1200-same/approve"]
    refused = js_async([PAGE_STUB, "core.js", "inbox.js"], SEND_STUBS + """(async () => {
      ui.repo = "b"; ui.multi = true;
      const sent = await inboxSend(args, "approve", {target: "plan"});
      return [sent, recorded, inboxDraft(args).error]; })()""", item)
    assert refused == [False, [], "This card names no repository; reload the page."]


def test_typing_and_arming_one_card_never_touch_the_other_with_the_same_run_and_key():
    item = {"run_id": "r", "key": "k", "kind": "blocked", "approve_note": "optional", "actions": ["approve"]}
    got = js([PAGE_STUB, "core.js", "inbox.js"], """(() => {
      const a = {repo: "a", ...args}, b = {repo: "b", ...args};
      inboxDraft(a).note = "only for a"; ui.inbox.armed = inboxItemKey(a);
      return [inboxDraft(b).note, ui.inbox.armed === inboxItemKey(b), inboxDraft(a).note, ui.inbox.drafts.size]; })()""", item)
    assert got == ["", False, "only for a", 2]


def test_approve_all_posts_each_approval_to_its_own_repository():
    got = js_async(["inbox.js"], """(async () => {
      var sent = [];
      setTimeout = () => 0; clearTimeout = () => {};
      renderInbox = () => {};
      act = async (run, action, body, repo) => { sent.push(run + ":" + repo); return true; };
      viewedList = () => [];
      ui = {multi: true, repo: "c", inbox: {view: {items: args}, armed: null, armedAt: 0, drafts: new Map(), busy: new Set(),
                    sent: new Set()}};
      approveAllItems(); approveAllItems();
      for (let i = 0; i < 50; i++) await null;
      return sent;
    })()""", [approval("r1", repo="a"), approval("r2", phase="plan", repo="b")])
    assert got == ["r1:a", "r2:b"]


def test_a_phase_approval_carries_what_its_own_repository_showed():
    item = {"repo": "a", "run_id": "r", "kind": "phase_approval", "phase": "plan", "key": "plan_approved", "approve_note": "none"}
    got = js(["inbox.js"], "(viewedList = (id, repo) => [{path: id, sha: 'abcdef1', repo: repo}], approvePayload(args, {note: ''}))", item)
    assert got["body"]["viewed"] == [{"path": "r", "sha": "abcdef1", "repo": "a"}]


def test_agents_of_two_repositories_with_one_run_id_are_two_groups():
    rows = [{"repo": "a", "run_id": "r", "module": "x", "idle": False, "age_s": 5},
            {"repo": "b", "run_id": "r", "module": "x", "idle": False, "age_s": 9},
            {"repo": "a", "run_id": "r", "module": "x", "idle": True, "age_s": 1}]
    got = js([PAGE_STUB, "core.js", "agents.js"], "agentsByRun(args).map(g => [g.repo, g.rows.map(r => r.age_s)])", rows)
    assert got == [["a", [1, 5]], ["b", [9]]]


def test_the_header_inbox_count_is_only_set_from_a_view_on_a_page_of_one():
    assert js(["inbox.js"], "[inboxBadgeCount({items: [1, 2]}, false), inboxBadgeCount({items: [1, 2]}, true)]", None) == [2, None]


def test_a_repositorys_failed_read_is_a_line_of_text_naming_it():
    errors = [{"repo": "a", "error": "events.jsonl: bad line 3"}, {"repo": "gone", "error": "<b>x</b>"}]
    got = js([PAGE_STUB, "core.js", "repos.js"], """(() => {
      ui.repos = {repos: [{id: "a", name: "api", state: "live"}]};
      return [viewErrorLines(args), viewErrorLines(undefined), viewErrorLines([])]; })()""", errors)
    assert got == [["api: events.jsonl: bad line 3", "gone: <b>x</b>"], [], []]


def test_the_agents_table_leads_with_the_repository_only_when_asked():
    rows = [{"repo": "a", "run_id": "r", "module": "m", "idle": False, "age_s": 5, "place": "build", "subagents": []}]
    got = js([PAGE_STUB, "core.js", "repos.js", "agents.js"], """(() => {
      routeHash = () => "#"; goRun = () => {}; SESSIONS_TAB = "sessions";
      el = (tag, attrs, ...kids) => ({tag: tag, attrs: attrs || {}, kids: kids.flat(Infinity)});
      ui.repos = {repos: [{id: "a", name: "api", state: "live"}]};
      const text = (n) => (n && typeof n === "object" ? (n.attrs && n.attrs.text) || "" : "") + (n && n.kids ? n.kids.map(text).join("|") : "");
      const heads = (t) => t.kids[0].kids[0].kids.map(text);
      return [heads(agentTable(args, false, true)), heads(agentTable(args, false, false)), heads(agentTable(args, false))]; })()""", rows)
    assert got[0][0] == "Repository" and got[0][1] == "Session" and len(got[0]) == len(got[1]) + 1
    assert got[1] == got[2] and got[1][0] == "Session"


# ---- the final fix wave: a broken Board never freezes the merged Inbox and Agents; a switch closes a modal; a rescan refetches tagged
def test_a_failing_board_does_not_stop_the_merged_inbox_from_drawing():  # Review Focus 4
    got = js_async(["repos.js"], """(async () => {
      const dirty = {board: false, agents: false, inbox: false}, drawn = [], failed = [];
      const draw = {board: (v) => drawn.push("board"), inbox: (v) => drawn.push("inbox:" + v.items.length), agents: (v) => drawn.push("agents"),
                    workflow: (v) => drawn.push("workflow"), fail: (name, err) => failed.push(name + ":" + err.message)};
      const conflict = () => { const e = new Error("pack broke on line 3"); e.status = 409; return Promise.reject(e); };
      const got = await settleRound({board: conflict(), agents: Promise.resolve({live: []}), workflow: conflict(), inbox: Promise.resolve({items: [1]})});
      drawRound(got, dirty, draw, true);
      return [drawn, failed, dirty]; })()""", None)
    assert got == [["agents", "inbox:1"], ["board:pack broke on line 3", "workflow:pack broke on line 3"],
                   {"board": True, "agents": False, "inbox": False}]


def test_a_page_of_one_keeps_a_failed_round_whole():
    got = js_async(["repos.js"], """(async () => {
      const dirty = {board: false, agents: false, inbox: false}, drawn = [], failed = [];
      const draw = {board: () => drawn.push("board"), inbox: () => drawn.push("inbox"), agents: () => drawn.push("agents"),
                    workflow: () => drawn.push("workflow"), fail: (name, err) => failed.push(name + ":" + err.message)};
      const got = await settleRound({board: Promise.resolve({}), agents: Promise.reject(new Error("HTTP 500")), workflow: null, inbox: Promise.resolve({})});
      drawRound(got, dirty, draw, false);
      return [drawn, failed, dirty]; })()""", None)
    assert got == [[], ["board:HTTP 500"], {"board": True, "agents": True, "inbox": True}]  # nothing drawn, all asked marked again


def test_a_part_that_throws_while_drawing_is_that_parts_failure_only():
    got = js(["repos.js"], """(() => {
      const dirty = {board: false, agents: false, inbox: false}, drawn = [], failed = [];
      const draw = {board: () => { throw new Error("bad card"); }, inbox: () => drawn.push("inbox"), agents: () => {}, workflow: () => {},
                    fail: (name, err) => failed.push(name + ":" + err.message)};
      drawRound({board: {v: {}}, inbox: {v: {}}}, dirty, draw, true);
      return [drawn, failed, dirty]; })()""", None)
    assert got == [["inbox"], ["board:bad card"], {"board": True, "agents": False, "inbox": False}]


APPLY_STUBS = """var settings = {request: 0, data: null, edits: new Map(), yamlDirty: false}, lastActivity = new Map(), dirty = {runs: new Set()};
  var closed = [], els = {dlg: {open: true, close() { this.open = false; closed.push("dlg"); }}, columns: {replaceChildren() {}},
    needs: {replaceChildren() {}}, summary: {}};
  document.getElementById = (id) => els[id] || null;
  closePeek = () => {}; disarmBacklog = () => {}; renderRepos = () => {}; loadInfo = () => {}; boardRefresh = () => {};"""


def test_a_repository_switch_closes_an_open_dialog():
    got = js([PAGE_STUB, "core.js", "repos.js"], APPLY_STUBS + """
      (() => {
        ui.repo = "a"; applyRepo("b"); const first = [els.dlg.open, closed.length];
        els.dlg.open = false; applyRepo("a");  // a closed dialog is left alone
        return [first, closed.length]; })()""", None)
    assert got == [[False, 1], 1]


def test_a_dialogs_answer_after_a_switch_closes_nothing_and_still_floats_its_message():
    got = js_async([PAGE_STUB, "core.js", "dialogs.js"], """(async () => {
      const made = {};
      const node = (id) => ({id: id, textContent: "", hidden: false, open: false, classList: {toggle() {}}, replaceChildren() {}, replaceWith(n) { made[id] = n; },
        cloneNode() { const c = node(id); c.handlers = {}; c.addEventListener = (t, f) => { c.handlers[t] = f; }; return c; },
        showModal() { this.open = true; }, close() { this.open = false; }});
      for (const id of ["dlg", "dlg-title", "dlg-body", "dlg-ok", "dlg-cancel", "dlg-msg"]) made[id] = node(id);
      document.getElementById = (id) => made[id];
      var floated = [], closes = 0, release;
      toast = (t) => floated.push(t);
      const dlg = made.dlg; const realClose = dlg.close; dlg.close = function () { closes++; realClose.call(this); };
      dialog("t", [], () => new Promise((resolve) => { release = resolve; }), "OK");
      const click = made["dlg-ok"].handlers.click();  // OK pressed: the action is in flight
      dlg.close();  // the repository switch closes the dialog
      dialogSaid = "recorded elsewhere";  // what the in-flight action said meanwhile
      release(true);
      await click;
      return [dlg.open, closes, floated]; })()""", None)
    assert got == [False, 1, ["recorded elsewhere"]]  # the late answer closed nothing again and threw nothing


def test_a_rescan_that_adds_a_second_repository_refetches_the_inbox_tagged():
    got = js_async([PAGE_STUB, "core.js", "repos.js"], """var dirty = {inbox: false, agents: false, board: false, runs: new Set()}, refreshed = 0;
    (async () => {
      parseRoute = () => ({repo: null}); setInboxBadge = () => {}; renderRepos = () => {}; renderAllChips = () => {}; routeHash = () => "#/inbox";
      boardRefresh = () => { refreshed++; };
      const one = {repos: [{id: "a", name: "a", state: "live", waiting: 0}], skipped: [], default: "a"};
      const two = {repos: [{id: "a", name: "a", state: "live", waiting: 0}, {id: "b", name: "b", state: "live", waiting: 0}], skipped: [], default: "a"};
      var answer = one; api = async () => answer;
      await loadRepos();  // the page's first answer: nothing to refetch
      const first = [dirty.inbox, dirty.agents, refreshed];
      answer = two; await loadRepos();
      return [first, [dirty.inbox, dirty.agents, refreshed], ui.repo, ui.multi]; })()""", None)
    assert got == [[False, False, 0], [True, True, 1], "a", True]


# ---- two-level Settings: Your settings, the save badges and the empty start (W4)
USER_UI = [DOM_STUB, "repos.js", "settings.js", "user_settings.js"]
FORM_STUB = str(Path(__file__).parent / "fixtures" / "js" / "_form_stub.js")
USER_FORM = [FORM_STUB, "repos.js", "settings.js", "user_settings.js"]
# An element that keeps its listeners, so a test can type into a field and read the edit it makes.
REC_EL = ("el = (tag, p, ...c) => { const n = {tag, p: p || {}, c: c.flat().filter((x) => x), on: {}, classList: {toggle() {}},"
          " addEventListener(t, f) { n.on[t] = f; }}; return n; }; globalThis.Node = Object;"
          "var updated = 0; var scope = {edits: new Map(), rows: new Map(), update: () => { updated++; }};")


def test_each_settings_field_says_where_it_saves():
    assert js([DOM_STUB, "settings.js"], "saveBadge({target: 'user'}, 'billing')", None) == "saves to: your user config"
    assert js([DOM_STUB, "settings.js"], "saveBadge({target: 'project'}, 'billing')", None) == "saves to: billing"


def test_a_lines_field_sends_one_entry_per_line():
    assert js([DOM_STUB, "settings.js"], "linesValue(args)", " ~/a \n\n/b\n") == ["~/a", "/b"]
    assert js([DOM_STUB, "settings.js"], "linesValue(args)", "") == []


def test_an_empty_workspace_starts_on_settings():
    got = js(USER_UI, "[emptyStart({repos: []}), emptyStart({repos: [{id: 'a'}]}), emptyStart(null), emptyStart({})]", None)
    assert got == [True, False, False, False]


def test_a_user_save_says_what_needs_a_restart():
    port = js(USER_UI, "userSaveNote({restart: ['dashboard.port'], rescanned: false})", None)
    assert "dashboard.port" in port and "restart" in port
    assert "scanned" in js(USER_UI, "userSaveNote({restart: [], rescanned: true})", None)
    assert js(USER_UI, "userSaveNote({restart: [], rescanned: false})", None) == "Saved to your user config"


def test_unsaved_user_edits_never_hold_up_a_repository_switch():
    got = js(USER_UI, """ui = {repos: null, repo: 'a'}; userSettings.edits.set('k', {key: ['dashboard', 'port'], value: 1});
      confirm = () => { throw new Error('asked'); }; [settingsMayLeave(), userSettings.edits.size, settings.edits.size]""", None)
    assert got == [True, 1, 0]


def test_a_lines_field_is_a_textarea_that_sends_its_lines_to_its_own_scope():
    field = {"key": ["workspace", "roots"], "label": "Roots", "kind": "list", "lines": True, "value": ["~/a"], "present": True,
             "source": "user", "optional": True, "target": "user"}
    got = js(USER_UI, REC_EL + """const row = fieldRow(args, scope); const box = row.c[1];
      box.value = " ~/a \\n\\n/b\\n"; box.on.input(); const sent = [...scope.edits.values()];
      box.value = "~/a"; box.on.input(); const back = scope.edits.size;
      box.value = ""; box.on.input();
      [box.tag, box.p.class, box.p.id, sent, back, [...scope.edits.values()], settings.edits.size, updated]""", field)
    assert got[:3] == ["textarea", "lines", "ucfg-workspace-roots"]
    assert got[3] == [{"key": ["workspace", "roots"], "value": ["~/a", "/b"]}]
    assert got[4] == 0  # back to the value it had: no edit
    assert got[5] == [{"key": ["workspace", "roots"], "value": None}]  # cleared: your value is deleted
    assert got[6:] == [0, 3]  # the repository form's edits are untouched


def test_a_cleared_user_field_the_file_does_not_set_is_not_sent():
    field = {"key": ["workspace", "roots"], "label": "Roots", "kind": "list", "lines": True, "value": [], "present": False,
             "source": "engine", "optional": True, "target": "user"}
    got = js(USER_UI, REC_EL + """const box = fieldRow(args, scope).c[1];
      box.value = "\\n  \\n"; box.on.input(); const blank = scope.edits.size;
      const port = fieldRow({key: ['dashboard', 'port'], label: 'Port', kind: 'int', value: 8765, present: false, source: 'engine',
                             optional: true, target: 'user'}, scope).c[1];
      port.value = ""; port.on.input(); [blank, scope.edits.size, updated]""", field)
    assert got == [0, 0, 2]


def test_a_field_row_names_where_it_saves_and_points_a_user_set_field_to_your_settings():
    ro = "set in your user config (/home/u/.config/agentflow/config.yaml), which overrides the pack"
    field = {"key": ["dashboard", "port"], "label": "Port", "kind": "int", "value": 9000, "source": "user", "target": "project",
             "readonly": ro, "hint": "change it under Your settings"}
    texts = js(USER_UI, REC_EL + """ui = {repos: {repos: [{id: 'billing-1a2b', name: 'billing', state: 'live'}]}, repo: null, multi: false};
      const row = fieldRow(args, scope);
      const all = []; const walk = (n) => { if (n.p && n.p.text !== undefined) all.push(n.p.text); (n.c || []).forEach(walk); }; walk(row);
      [all, row.c[1].p.id, row.c[1].p.disabled]""", field)
    assert texts[1] == "cfg-dashboard-port" and texts[2] is True
    assert "saves to: billing" in texts[0]
    assert texts[0].index(ro) < texts[0].index("change it under Your settings")  # the hint follows the read-only note
    user = js(USER_UI, REC_EL + "ui = {repos: null, repo: null}; fieldRow(args, scope).c.map((n) => n.p.text)",
              {**field, "target": "user", "readonly": None, "hint": None})
    assert "saves to: your user config" in user


def test_a_user_config_that_cannot_be_edited_renders_every_field_disabled():
    data = {"path": "/u/config.yaml", "sha256": "0" * 64, "exists": True, "editable": False, "error": "it is a symlink; fix it by hand",
            "rescans": True, "sections": [{"title": "Workspace", "note": "n", "fields": [
                {"key": ["workspace", "roots"], "label": "Roots", "kind": "list", "lines": True, "value": [], "present": False,
                 "source": "engine", "optional": True, "target": "user"},
                {"key": ["dashboard", "port"], "label": "Port", "kind": "int", "value": 8765, "present": False,
                 "source": "engine", "optional": True, "target": "user"}]}]}
    got = js(USER_FORM, REC_EL + """ui = {repos: {repos: [{id: 'a'}]}, repo: null};
      userSettings.data = args; renderUserSettings();
      const inputs = nodes['user-form'].kids[0].c.slice(2).map((row) => row.c[1]);
      [inputs.map((i) => i.p.disabled), nodes['user-status'].textContent, nodes['user-review'].disabled, nodes['user-path'].textContent]""",
             data)
    assert got[0] == [True, True]
    assert "it is a symlink; fix it by hand" in got[1]
    assert got[2] is True and got[3] == "/u/config.yaml"


def test_a_user_save_reloads_the_form_and_the_repositories_and_wakes_an_empty_page():
    got = js_async(USER_FORM, """var asked = [], toasts = [], calls = [], ok = null;
      ui = {repos: {repos: []}, multi: false, repo: null, workflow: {x: 1}, view: 'board'};
      toast = (t) => toasts.push(t); diffNode = () => null; dialog = (title, body, onOk) => { ok = onOk; };
      el = () => ({}); renderUserSettings = () => calls.push('render');
      loadRepos = async () => { calls.push('repos'); ui.repos = {repos: [{id: 'a', state: 'live'}]}; };
      loadInfo = () => calls.push('info'); boardRefresh = () => calls.push('board');
      api = async (path, body, repo) => { asked.push([path, repo === null]);
        if (path.endsWith('/preview')) return {changed: true, ok: true, diff: '', issues: []};
        if (path.endsWith('/save')) return {ok: true, sha256: 'b', restart: [], rescanned: true};
        return {sha256: 'b', editable: true, sections: []}; };
      (async () => {
        userSettings.data = {sha256: 'a', editable: true, sections: []};
        userSettings.edits.set('k', {key: ['workspace', 'roots'], value: ['~/code']});
        await reviewUserSettings(); const done = await ok();
        return [done, asked, toasts, calls, ui.workflow, userSettings.edits.size]; })()""", None)
    done, asked, toasts, calls, workflow, edits = got
    assert done is True and workflow is None and edits == 0
    assert asked == [["/api/user-config/preview", True], ["/api/user-config/save", True], ["/api/user-config", True]]
    assert toasts == ["Saved to your user config; the workspace was scanned again"]
    assert calls == ["render", "repos", "info", "board"]  # none to some: the page wakes up


def test_an_empty_start_asks_for_a_folder_and_hides_the_repository_form():
    data = {"path": "/u/config.yaml", "sha256": "0" * 64, "exists": False, "editable": True, "error": None, "rescans": True,
            "sections": [{"title": "Workspace", "note": "n", "fields": [
                {"key": ["workspace", "roots"], "label": "Roots", "kind": "list", "lines": True, "value": [], "present": False,
                 "source": "engine", "optional": True, "target": "user"}]}]}
    got = js(USER_FORM, REC_EL + """var asked = []; api = async (path) => { asked.push(path); return {}; };
      ui = {repos: {repos: []}, multi: false, repo: null}; userSettings.focusRoots = true;
      userSettings.data = args; renderUserSettings(); showRepoSettings();
      const empty = [nodes['user-prompt'].hidden, !!nodes['ucfg-workspace-roots'].focused, userSettings.focusRoots,
                     nodes['settings-repo'].hidden, asked.length];
      ui.repos = {repos: [{id: 'billing-1a2b', name: 'billing', state: 'live'}]}; renderUserSettings(); showRepoSettings();
      [empty, [nodes['user-prompt'].hidden, nodes['settings-repo'].hidden, nodes['settings-repo-title'].textContent, asked]]""", data)
    assert got[0] == [False, True, False, True, 0]  # no repository: the prompt, the cursor in the roots, no /api/config
    assert got[1] == [True, False, "Repository: billing", ["/api/config"]]


def test_the_folder_prompt_goes_once_a_save_lists_a_repository():
    got = js_async(USER_FORM, REC_EL + """var ok = null;
      ui = {repos: {repos: []}, multi: false, repo: null, workflow: null, view: 'board'};
      toast = () => {}; diffNode = () => null; dialog = (title, body, onOk) => { ok = onOk; };
      loadRepos = async () => { ui.repos = {repos: [{id: 'a', name: 'a', state: 'live'}]}; };
      loadInfo = () => {}; boardRefresh = () => {};
      api = async (path) => path.endsWith('/preview') ? {changed: true, ok: true, diff: '', issues: []}
        : path.endsWith('/save') ? {ok: true, sha256: 'b', restart: [], rescanned: true}
        : {sha256: 'b', exists: true, editable: true, error: null, rescans: true, sections: []};
      (async () => {
        userSettings.data = {sha256: 'a', exists: false, editable: true, error: null, rescans: true, sections: []};
        renderUserSettings(); const before = nodes['user-prompt'].hidden;
        userSettings.edits.set('k', {key: ['workspace', 'roots'], value: ['~/code']});
        await reviewUserSettings(); await ok();
        return [before, nodes['user-prompt'].hidden]; })()""", None)
    assert got == [False, True]  # asked for a folder; gone once the save listed its repository
