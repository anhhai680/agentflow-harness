from __future__ import annotations

import pytest

from agentflow import transcripts
from agentflow.run_files import read_range
from agentflow.sessions import find_session, transcript_file
from agentflow.transcripts import parse_lines, transcript_page
from conftest import L, session_with_transcript


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


def test_user_text_and_unknown_blocks_become_capped_raw_turns():
    notice = "This session is being continued from a previous conversation"
    turns = parse_lines([
        L({"type": "user", "message": {"content": [{"type": "text", "text": notice}]}, "parent_tool_use_id": "p1"}),
        L({"type": "user", "message": {"content": "a plain string " + "x" * 50000}}),
        L({"type": "assistant", "message": {"content": [{"type": "redacted_thinking", "data": "abc"}]}})])
    assert [t["kind"] for t in turns] == ["raw", "raw", "raw"]
    assert turns[0]["text"] == notice and turns[0]["parent"] == "p1"
    assert turns[1]["text"].startswith("a plain string") and len(turns[1]["text"]) < 20000
    assert "redacted_thinking" in turns[2]["text"] and turns[2]["parent"] is None


def test_codex_items_and_unknown_lines_become_turns():
    turns = parse_lines([L({"type": "thread.started", "thread_id": "c"}),
                         L({"type": "item.completed", "item": {"type": "command_execution", "command": "ls",
                                                                "aggregated_output": "a\nb"}}),
                         b"not json", L({"type": "brand_new_type", "x": 1})])
    assert [t["kind"] for t in turns] == ["session", "tool", "raw", "raw"] and turns[1]["line"] == "command: ls"
    assert turns[1]["name"] == "command" and turns[1]["result"] == "a\nb"


def test_codex_progress_records_fold_into_waits():
    turns = parse_lines([L({"type": "turn.started"}), L({"type": "item.started", "item": {"type": "x"}}),
                         L({"type": "item.updated", "item": {"type": "x"}}),
                         L({"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}}),
                         L({"type": "turn.completed", "usage": {}})])
    assert [t["kind"] for t in turns] == ["wait", "text", "wait"] and turns[0]["records"] == 3


def test_cut_text_says_how_much_was_dropped_and_tool_lines_are_summarised():
    turns = parse_lines([L({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "a" * (transcripts.TEXT_MAX + 5)},
        {"type": "tool_use", "id": "t", "name": "Glob", "input": {"pattern": "*.py"}},
        {"type": "tool_use", "id": "u", "name": "Custom", "input": {"n": 1, "s": "first"}}]}})])
    assert turns[0]["text"].endswith(" … (5 more characters)") and len(turns[0]["text"]) > transcripts.TEXT_MAX
    assert turns[1]["line"] == "Glob: *.py" and turns[2]["line"] == "Custom: first"


def test_a_file_holding_a_sizer_and_attempt_one_reads_as_two_sessions():
    init = L({"type": "system", "subtype": "init", "model": "haiku"})
    assert [t["kind"] for t in parse_lines([init, L({"type": "result", "result": "high"}), init])] == \
        ["session", "result", "session"]


def test_pages_return_every_line_of_a_5_mb_transcript(flow_repo):
    run, sid = session_with_transcript(flow_repo, lines=20000, width=250)
    after, seen = 0, 0
    while True:
        page = transcript_page(run, sid, after, 512 * 1024)
        assert page["next"] - page["after"] <= 512 * 1024
        seen += sum(1 for t in page["turns"] if t["kind"] == "text")
        after = page["next"]
        if page["done"]:
            break
    assert seen == 20000


def test_a_line_larger_than_the_page_is_skipped_not_stuck(flow_repo):
    run, sid = session_with_transcript(flow_repo, raw=[L({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "x" * (3 << 20)}]}}), L({"type": "result", "result": "ok"})])
    first = transcript_page(run, sid, 0, 512 * 1024)
    assert first["turns"][0]["kind"] == "skipped" and first["turns"][0]["bytes"] > 3 << 20
    rest = transcript_page(run, sid, first["next"], 512 * 1024)
    assert [t["kind"] for t in first["turns"] + rest["turns"]] == ["skipped", "result"]


def test_a_long_line_still_being_written_is_not_skipped(flow_repo):
    run, sid = session_with_transcript(flow_repo, raw=[L({"type": "result", "result": "a"})], tail=b"y" * 3000)
    first = transcript_page(run, sid, 0, 1024)
    assert [t["kind"] for t in first["turns"]] == ["result"]
    second = transcript_page(run, sid, first["next"], 1024)
    assert second["turns"] == [] and second["next"] == first["next"] and second["next"] < second["size"]


def test_a_line_still_being_written_waits_for_its_newline(flow_repo):
    run, sid = session_with_transcript(flow_repo, raw=[L({"type": "result", "result": "a"})], tail=b'{"type": "res')
    page = transcript_page(run, sid, 0, 512 * 1024)
    assert len(page["turns"]) == 1 and page["next"] < page["size"] and not page["done"]


def test_a_symlinked_transcript_or_a_bad_cursor_is_refused(flow_repo, tmp_path):
    run, sid = session_with_transcript(flow_repo, raw=[L({"type": "result", "result": "ok"})])
    path = transcript_file(run, find_session(run, sid))
    size = path.stat().st_size
    for after in (3, size + 1):
        with pytest.raises(ValueError, match="page boundary"):
            transcript_page(run, sid, after, 512 * 1024)
    (tmp_path / "secret.jsonl").write_bytes(L({"type": "result", "result": "secret"}) + b"\n")
    path.unlink()
    path.symlink_to(tmp_path / "secret.jsonl")
    page = transcript_page(run, sid, 0, 512 * 1024)
    assert page["missing"] is True and page["turns"] == [] and page["done"] is True


def test_the_cache_is_bounded_and_hands_out_copies(flow_repo):
    run, sid = session_with_transcript(flow_repo, raw=[L({"type": "result", "result": "ok"})])
    page = transcript_page(run, sid, 0, 4096)
    page["turns"][0]["text"] = "mutated"
    assert transcript_page(run, sid, 0, 4096)["turns"][0]["text"] == "ok"
    for n in range(100, 100 + 2 * transcripts.CACHE_MAX):
        transcript_page(run, sid, 0, n)
    assert len(transcripts._cache) <= transcripts.CACHE_MAX


def test_read_range_seeks_and_keeps_the_head_rules(tmp_path):
    (tmp_path / "f").write_bytes(b"0123456789")
    assert read_range(tmp_path, tmp_path / "f", 4, 3) == (b"456", 10)
    assert read_range(tmp_path, tmp_path / "none", 0, 3) is None
    (tmp_path / "l").symlink_to(tmp_path / "f")
    assert read_range(tmp_path, tmp_path / "l", 0, 3) is None


def test_a_deeply_nested_line_becomes_a_capped_raw_turn_not_an_error():
    deep = b"[" * 100000 + b"]" * 100000
    nested_input = b'{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t", "name": "X", "input": ' \
        + deep + b"}]}}"
    nested_unknown = b'{"type": "brand_new_type", "x": ' + deep + b"}"
    turns = parse_lines([deep, nested_input, nested_unknown, L({"type": "result", "result": "ok"})])
    assert turns[0]["kind"] == "raw" and len(turns[0]["text"]) < transcripts.IO_MAX + 100
    assert turns[-1]["kind"] == "result" and len(turns) == 4
