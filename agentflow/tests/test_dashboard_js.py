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
