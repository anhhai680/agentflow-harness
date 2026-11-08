"""The lessons file: its strict format (`parse`/`render`), the two layers (`load`) and what a step's prompt carries
(`select`)."""

from __future__ import annotations

import hashlib
from datetime import date

import pytest

from agentflow import lessons, pack
from agentflow.config import load_config, validate
from agentflow.lessons import LESSONS_HEADING, SOURCE, Lesson, Selection, estimate_tokens, load, parse, render, select

TODAY = date(2026, 11, 8)


def _lesson(lid: str, title: str = "t", body: str = "b", applies: tuple[str, ...] = (),
            review_by: date | None = None, target: str | None = None) -> Lesson:
    return Lesson(lid, title, body, applies, (), review_by, target, None)


E1 = _lesson("E-0001", "Grep first", "Grep a big document by heading before reading it.")
L1_build = _lesson("L-0001", "Run lint", "Run the linter before reporting a task done.", ("build",))
L2_plan = _lesson("L-0002", "Name tests", "Each plan task names its test file.", ("plan",))
L3_expired = _lesson("L-0003", "Old", "This one is past its review date.", (), date(2026, 11, 7))
L4_build = _lesson("L-0004", "Format", "Format the code before reporting a task done.", ("build", "verify"))


# --------------------------------------------------------------------------- parse and render


def test_round_trip():
    ls = [Lesson("L-0001", "Run lint", "Run `ruff` first.", ("build",), ("20261105-0912-feature-x#4",),
                 date(2027, 2, 1), "gate_first_pass:lint", "0.33 over 9 runs to 2026-11-08")]
    assert parse(render("# Lessons\n", ls), "L") == (ls, [])


def test_round_trip_of_several_lessons_with_and_without_keys():
    ls = [L1_build, L3_expired, L4_build, Lesson("L-0009", "Two sources", "Body {braces} stay.", ("build", "verify"),
                                                ("20261105-0912-feature-x#4", "20261106-1400-bug-y#77"), None, None,
                                                None)]
    assert parse(render("# Lessons\n\nA header paragraph.\n", ls), "L") == (ls, [])


def test_malformed_entries_are_skipped_and_reported():
    text = "## L-0001 ok\n- review_by: 2027-01-01\nbody\n\n## L-0001 dup\nbody\n\n## L-x bad id\nbody\n\nstray prose\n"
    lessons_, problems = parse(text.replace("\n", "\r\n"), "L")
    assert [x.id for x in lessons_] == ["L-0001"] and len(problems) == 3


def test_render_neutralises_headings_and_invisible_text():
    ls = [Lesson("L-0002", "t", "## L-0099 evil​", (), (), None, None, None)]
    again, problems = parse(render("", ls), "L")
    assert [x.id for x in again] == ["L-0002"] and again[0].body == "L-0099 evil" and not problems


def test_render_keeps_a_multi_line_or_list_like_body_on_one_line():
    ls = [Lesson("L-0003", "# also a heading", "- applies: plan\n## L-0004 forged\nsecond line‮", (), (),
                 None, None, None)]
    text = render("", ls)
    again, problems = parse(text, "L")
    assert not problems and [x.id for x in again] == ["L-0003"]
    assert again[0].title == "also a heading" and again[0].applies == ()
    assert again[0].body == "applies: plan ## L-0004 forged second line"
    assert [line for line in text.splitlines() if line.startswith("## ")] == ["## L-0003 also a heading"]


def test_render_values_cannot_add_a_comment_or_a_line():
    ls = [Lesson("L-0005", "t", "b", (), (), None, None, "3/9 runs # not a comment\n- target: stop:x")]
    again, problems = parse(render("", ls), "L")
    assert not problems and again[0].target is None
    assert again[0].baseline == "3/9 runs not a comment - target: stop:x"


def test_braces_are_left_verbatim():
    ls = [Lesson("L-0006", "{run}", "Use {brief} here.", (), (), None, None, None)]
    assert parse(render("", ls), "L")[0] == ls


@pytest.mark.parametrize("entry,fragment", [
    ("## L-0001\nbody\n", "no title"),
    ("## L-0001 t\n- review_by: 2027-13-01\nbody\n", "review_by"),
    ("## L-0001 t\n- colour: red\nbody\n", "unknown key"),
    ("## L-0001 t\n- applies: build\n- applies: plan\nbody\n", "twice"),
    ("## L-0001 t\n- source: 20261105-0912-x#notanumber\nbody\n", "source"),
    ("## L-0001 t\n- source: 20261105-0912-..#4\nbody\n", "source"),
    ("## L-0001 t\n- applies: build plan\nbody\n", "applies"),
    ("## L-0001 t\n- applies: build\n", "no body"),
    ("## L-0001 t\nbody\nmore body\n", "more than one body line"),
    ("## L-0001 t\nbody\n- applies: build\n", "more than one body line"),
    ("## L-0001 t\n" + "x" * 301 + "\n", "300"),
    ("## E-0001 t\nbody\n", "id"),
    ("## L-00001 t\nbody\n", "id"),
])
def test_each_malformed_entry_is_a_problem_and_is_skipped(entry, fragment):
    found, problems = parse("# header\n\n" + entry, "L")
    assert found == [] and len(problems) == 1 and fragment in problems[0], problems


def test_problems_name_the_line():
    _, problems = parse("# header\n\n## L-0001 t\n- colour: red\nbody\n", "L")
    assert problems[0].startswith("line 4: ")


def test_comment_after_a_key_is_ignored_but_a_hash_in_a_source_is_not():
    text = ("## L-0007 t\n- applies: build, verify   # phase names\n"
            "- source: 20261105-0912-feature-pack#418, 20261106-1400-bug-gates#77  # two runs\n"
            "- review_by: 2027-02-01\n- target: gate_first_pass:portability # a key\n"
            "- baseline: 3/9 runs (33%) to 2026-11-08 # written by the runner\nThe body.\n")
    (lesson,), problems = parse(text, "L")
    assert not problems
    assert lesson.applies == ("build", "verify")
    assert lesson.sources == ("20261105-0912-feature-pack#418", "20261106-1400-bug-gates#77")
    assert lesson.target == "gate_first_pass:portability" and lesson.baseline == "3/9 runs (33%) to 2026-11-08"


def test_parse_never_raises_on_junk():
    for text in ("", "\x00\x01", "## \n", "##\n- :\n", "- applies: x\n", "## L-0001 t\n\n\n", "﻿## L-0001 t\nb\n",
                 "## L-0001 t\n- review_by: 99999-01-01\nb\n", "## L-0001 t\n- source:\nb\n"):
        found, problems = parse(text, "L")
        assert isinstance(found, list) and isinstance(problems, list)


def test_text_before_the_first_entry_is_the_header():
    found, problems = parse("# Lessons\n\nOnly a merged retro PR or a human edits this file.\n- not a key\n", "L")
    assert (found, problems) == ([], [])


@pytest.mark.parametrize("value,ok", [
    ("20261105-0912-feature-x#4", True), ("20261105-0912-a.b_c-d#12", True),
    ("20261105-0912-..#4", False), ("20261105-0912-a..b#4", False), ("20261105-0912-a/b#4", False),
    ("20261105-0912-x#", False), ("2026110-0912-x#4", False), ("20261105-0912-x#4 ", False), ("20261105-0912-x#4\n", False),
    ("../x#4", False),
])
def test_source_pattern(value, ok):
    assert bool(SOURCE.match(value)) is ok


# --------------------------------------------------------------------------- select


def test_select_filters_by_phase_and_expiry_and_truncates_oldest_pack_first():
    sel = select([E1, L1_build, L2_plan, L3_expired], "build", TODAY, max_tokens=10_000)
    assert sel.ids == ("E-0001", "L-0001") and sel.dropped == ()
    two = select([E1, L4_build], "build", TODAY, max_tokens=10_000)
    tokens_for_two = estimate_tokens(two.text)
    assert estimate_tokens(select([E1, L1_build, L4_build], "build", TODAY, 10_000).text) > tokens_for_two
    tight = select([E1, L1_build, L4_build], "build", TODAY, max_tokens=tokens_for_two)
    assert tight.dropped == ("L-0001",) and tight.ids == ("E-0001", "L-0004") and tight.text == two.text


def test_select_drops_engine_lessons_last():
    e2 = _lesson("E-0002", "Second", "Another engine lesson.")
    only_engine = select([E1, e2], None, TODAY, 10_000)
    sel = select([E1, e2, L1_build, L4_build], "build", TODAY, estimate_tokens(only_engine.text))
    assert sel.ids == ("E-0001", "E-0002") and sel.dropped == ("L-0001", "L-0004")
    one = select([e2], None, TODAY, 10_000)
    sel = select([E1, e2, L1_build], "build", TODAY, estimate_tokens(one.text))
    assert sel.ids == ("E-0002",) and sel.dropped == ("L-0001", "E-0001")


def test_select_review_by_today_is_still_kept():
    assert select([_lesson("L-0001", review_by=TODAY)], None, TODAY, 10_000).ids == ("L-0001",)


def test_select_with_no_phase_keeps_only_lessons_for_every_phase():
    assert select([E1, L1_build], None, TODAY, 10_000).ids == ("E-0001",)


def test_select_renders_the_heading_lead_and_one_line_per_lesson():
    sel = select([E1, L1_build], "build", TODAY, 10_000)
    lines = sel.text.splitlines()
    assert lines[0] == LESSONS_HEADING
    assert ("Earlier runs of this project taught these. Follow them unless this step's own instructions or the rules "
            "above say otherwise.") in lines
    entries = [line for line in lines if line.startswith("- ")]
    assert entries == ["- **E-0001** Grep first: Grep a big document by heading before reading it.",
                       "- **L-0001** Run lint: Run the linter before reporting a task done."]
    assert sel.digest == hashlib.sha256("\n".join(entries).encode()).hexdigest()[:12]


def test_select_digest_changes_with_the_selection():
    a = select([E1], None, TODAY, 10_000).digest
    b = select([E1, _lesson("L-0001")], None, TODAY, 10_000).digest
    assert len(a) == 12 and a != b


def test_zero_budget_turns_lessons_off():
    assert select([E1], None, TODAY, 0).text == ""


def test_nothing_left_is_empty():
    assert select([L2_plan, L3_expired], "build", TODAY, 10_000) == Selection("", (), (), "")
    over = select([E1], None, TODAY, 1)
    assert over.text == "" and over.ids == () and over.dropped == ("E-0001",)


# --------------------------------------------------------------------------- the seed files and load


def test_engine_seed_parses_clean():
    found, problems = parse(pack.ENGINE_LESSONS.read_text(encoding="utf-8"), "E")
    assert problems == [] and 3 <= len(found) <= 6
    assert all(x.sources == () and x.target is None and x.review_by == date(2027, 11, 8) for x in found)


def test_this_repos_pack_lessons_parse_clean(repo_root):
    path = repo_root / pack.LESSONS_FILE
    assert parse(path.read_text(encoding="utf-8"), "L") == ([], [])


def test_load_reads_the_engine_then_the_pack(tmp_cfg):
    path = tmp_cfg()
    root = path.parent.parent
    from conftest import run_git
    run_git(root, "init", "-q", "-b", "main")
    assert [x.id for x in load(root)] == [x.id for x in parse(pack.ENGINE_LESSONS.read_text(), "E")[0]]
    (path.parent / "lessons.md").write_text("# Lessons\n\n## L-0001 t\nb\n\n## L-bad x\nb\n", encoding="utf-8")
    ids = [x.id for x in load(root)]
    assert ids[0].startswith("E-") and ids[-1] == "L-0001" and "L-bad" not in ids


def test_load_refuses_a_symlinked_pack_file(tmp_cfg, tmp_path):
    path = tmp_cfg()
    root = path.parent.parent
    from conftest import run_git
    run_git(root, "init", "-q", "-b", "main")
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("## L-0001 t\nb\n", encoding="utf-8")
    (path.parent / "lessons.md").symlink_to(elsewhere)
    assert not [x for x in load(root) if x.id.startswith("L-")]
    _, problems = lessons.read(path.parent / "lessons.md", "L")
    assert problems and "symlink" in problems[0]


# --------------------------------------------------------------------------- validate


def _lesson_warnings(tmp_cfg, text: str | None) -> list[str]:
    path = tmp_cfg()
    if text is not None:
        (path.parent / "lessons.md").write_text(text, encoding="utf-8")
    return [f"{i.level} {i.path}: {i.message}" for i in validate(load_config(path), path.parent.parent)
            if i.path == "lessons"]


def test_validate_with_no_pack_lessons_file_is_clean(tmp_cfg):
    assert _lesson_warnings(tmp_cfg, None) == []


def test_validate_reports_malformed_pack_lessons_as_warnings(tmp_cfg):
    found = _lesson_warnings(tmp_cfg, "# L\n\n## L-0001 t\n- colour: red\nb\n")
    assert found == ["warning lessons: .agentflow/lessons.md: line 4: L-0001 has an unknown key 'colour'"]


def test_validate_warns_on_an_unknown_target_and_phase(tmp_cfg):
    found = _lesson_warnings(tmp_cfg, "## L-0001 t\n- applies: build, nowhere\n- target: not_a_signal:x\nb\n")
    assert len(found) == 2 and all(w.startswith("warning lessons: .agentflow/lessons.md: L-0001 ") for w in found)
    assert any("not_a_signal:x" in w for w in found) and any("nowhere" in w for w in found)


def test_validate_gives_no_warning_for_a_passed_review_date(tmp_cfg):
    assert _lesson_warnings(tmp_cfg, "## L-0001 t\n- review_by: 2000-01-01\n- target: ci_rounds\nb\n") == []
