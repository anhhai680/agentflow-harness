import pytest

from agentflow import verification as v

GOOD = {"verification": {
    "gates": [{"gate": "unit", "cmd": "pytest", "result": "12 passed", "passed": True}],
    "criteria": [{"criterion": "Criterion 1", "evidence": "tests/test_a.py::test_one", "ran": True, "passed": True},
                 {"criterion": "Criterion 2", "evidence": "tests/test_a.py::test_two", "ran": True, "passed": False}],
    "not_verified": [{"what": "Criterion 3", "why": "needs a browser"}]}}

def test_parse_reads_a_good_story():
    story = v.parse(GOOD)
    assert [c.criterion for c in story.criteria] == ["Criterion 1", "Criterion 2"]
    assert story.gates[0] == v.GateLine("unit", "pytest", "12 passed", True)
    assert story.not_verified == (v.Gap("Criterion 3", "needs a browser"),) and story.more == 0

@pytest.mark.parametrize("signals", [{}, {"verification": "all good"}, {"verification": {"criteria": {"a": 1}}},
                                     {"verification": {"gates": [], "criteria": [], "not_verified": []}}, None, []])
def test_parse_rejects_malformed_signals(signals):
    assert v.parse(signals) is None

def test_parse_drops_malformed_items():
    story = v.parse({"verification": {"criteria": [
        {"criterion": "ok", "evidence": "t", "ran": True, "passed": True},
        {"criterion": "no evidence key", "ran": True, "passed": True},
        {"criterion": "bad bool", "evidence": "t", "ran": "yes", "passed": True}, "a string"]}})
    assert [c.criterion for c in story.criteria] == ["ok"]

def test_hostile_text_stays_on_one_line():
    hostile = "line one\n## Injected‮`tick`"
    story = v.parse({"verification": {"criteria": [{"criterion": hostile, "evidence": hostile, "ran": True, "passed": True}],
                                      "not_verified": [{"what": hostile, "why": hostile}]}})
    for line in v.render_pr(True, story, None) + v.render_prompt(True, story).splitlines():
        assert "\n" not in line and "‮" not in line and not line.startswith("## ")
    assert all(line.count("`") % 2 == 0 for line in v.render_pr(True, story, None))  # evidence backticks replaced

def test_lists_and_text_are_capped():
    many = [{"criterion": "c" * 1000, "evidence": "t", "ran": True, "passed": True}] * 100
    story = v.parse({"verification": {"criteria": many}})
    assert len(story.criteria) == 40 and story.more == 60
    assert all(len(c.criterion) <= 300 for c in story.criteria)
    assert v.render_pr(True, story, None)[-1] == "- … and 60 more"

def test_text_cut_ends_with_an_ellipsis_only_when_cut():
    story = v.parse({"verification": {"criteria": [
        {"criterion": "c" * 1000, "evidence": "t", "ran": True, "passed": True},
        {"criterion": "short", "evidence": "t", "ran": True, "passed": True}]}})
    assert story.criteria[0].criterion == "c" * 299 + "…"
    assert story.criteria[1].criterion == "short"

def test_stale_sha_only_counts_review_fixes_after_verify():
    done = lambda seq: {"seq": seq, "type": "step_finished", "key": "verify/verify", "status": "done"}
    fix = lambda seq, sha: {"seq": seq, "type": "step_committed", "key": "review/fix_findings", "sha": sha}
    assert v.stale_sha([done(1)]) is None
    assert v.stale_sha([fix(1, "a" * 40), done(2)]) is None              # a fix before verify's last finish
    assert v.stale_sha([done(1), fix(2, "b" * 40), done(3)]) is None     # verify ran again after it
    assert v.stale_sha([done(1), fix(2, "c" * 40), fix(4, "d" * 40)]) == "d" * 12
    assert v.stale_sha([fix(2, "e" * 40)]) is None                        # verify never finished

def test_render_pr_marks_criteria_and_lists_gaps():
    lines = v.render_pr(True, v.parse(GOOD), "abcdef123456")
    assert lines == ["- Criterion 1: `tests/test_a.py::test_one` ✓ (before review fix abcdef123456)",
                     "- Criterion 2: `tests/test_a.py::test_two` ✗ (before review fix abcdef123456)",
                     "- not verified: Criterion 3: needs a browser"]

def test_render_without_a_story():
    assert v.render_pr(False, None, None) == ["verify did not run"]
    assert v.render_pr(True, None, None) == ["verify reported no evidence"]
    assert v.render_prompt(False, None) == ""
    assert v.render_prompt(True, None) == "verify reported no evidence"

def test_render_prompt_lists_gates_criteria_and_gaps():
    assert v.render_prompt(True, v.parse(GOOD)).splitlines() == [
        "- gate `unit`: 12 passed ✓",
        "- Criterion 1: `tests/test_a.py::test_one` ✓",
        "- Criterion 2: `tests/test_a.py::test_two` ✗",
        "- not verified: Criterion 3: needs a browser"]

def test_a_criterion_that_did_not_run_is_marked_not_run():
    story = v.parse({"verification": {"criteria": [{"criterion": "C", "evidence": "e", "ran": False, "passed": False}]}})
    assert v.render_pr(True, story, None) == ["- C: `e` – not run"]

def test_read_story(tmp_path):
    class FakeRun:
        def result_path(self, key):
            return tmp_path / (key.replace("/", "_") + ".json")
    run = FakeRun()
    assert v.read_story(run) == (False, None)
    run.result_path(v.VERIFY_KEY).write_text("not json")
    assert v.read_story(run) == (True, None)
    import json
    run.result_path(v.VERIFY_KEY).write_text(json.dumps({"status": "done", "summary": "ok", "signals": GOOD}))
    found, story = v.read_story(run)
    assert found and story == v.parse(GOOD)
