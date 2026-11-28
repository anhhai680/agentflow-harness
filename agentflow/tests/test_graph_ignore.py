"""graph_ignore: a pure .gitignore matcher, cross-checked against `git check-ignore` itself."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from agentflow import graph_ignore as gi

ROOT = {"": "*.log\n/build/\nnode_modules/\n!keep.log\ndocs/**/*.tmp\n**/cache\nsecret?.txt\n[ab].dat\n#c.log\n\\#lit\n"}
CASES = [
    (ROOT, "a.log", True), (ROOT, "x/y/a.log", True), (ROOT, "keep.log", False), (ROOT, "x/keep.log", False),
    (ROOT, "build/out.js", True), (ROOT, "x/build/out.js", False), (ROOT, "node_modules/p/i.js", True),
    (ROOT, "x/node_modules/i.js", True), (ROOT, "docs/a/b/c.tmp", True), (ROOT, "docs/c.tmp", True),
    (ROOT, "x/cache", True), (ROOT, "x/cache/f", True), (ROOT, "secret1.txt", True), (ROOT, "secret12.txt", False),
    (ROOT, "a.dat", True), (ROOT, "c.dat", False), (ROOT, "c.log", True), (ROOT, "#lit", True),
    ({"": "out/\n!out/keep.txt\n"}, "out/keep.txt", True),              # cannot re-include under an ignored directory
    ({"": "*.gen\n", "pkg": "!api.gen\n"}, "pkg/api.gen", False),        # a deeper file wins
    ({"": "*.gen\n", "pkg": "!api.gen\n"}, "other/api.gen", True),       # and applies below its directory only
    ({"pkg": "/local\n"}, "pkg/local", True), ({"pkg": "/local\n"}, "pkg/x/local", False),
    ({"": "name/\n"}, "name", False),                                    # a file named like a directory-only pattern
    # extra rows: trailing spaces, classes, negated classes, a trailing /**, a middle **, an escaped space
    ({"": "a b \n"}, "a b", True), ({"": "a\\ \n"}, "a ", True), ({"": "a\\ \n"}, "a", False),
    ({"": "[!a-c]x\n"}, "dx", True), ({"": "[!a-c]x\n"}, "bx", False), ({"": "[a-c]x\n"}, "bx", True),
    ({"": "[]]x\n"}, "]x", True), ({"": "logs/**\n"}, "logs/a/b", True), ({"": "logs/**\n"}, "logs", False),
    ({"": "a/**/b\n"}, "a/b", True), ({"": "a/**/b\n"}, "a/x/y/b", True), ({"": "a/**/b\n"}, "x/a/b", False),
    ({"": "**\n"}, "any/thing", True), ({"": "a**b\n"}, "aXXb", True), ({"": "a**b\n"}, "a/b", False),
    ({"": "*\n!*/\n!*.py\n"}, "pkg/m.py", False), ({"": "*\n!*/\n!*.py\n"}, "pkg/m.txt", True),
    ({"": "/a/b\n"}, "a/b", True), ({"": "a/b\n"}, "x/a/b", False), ({"": "foo\n!foo/bar\n"}, "foo/bar", True),
    # CRLF line endings and a leading byte order mark, as git reads them
    ({"": "secrets/\r\n*.log\r\n"}, "secrets/a.py", True), ({"": "secrets/\r\n*.log\r\n"}, "a.log", True),
    ({"": "*.log\r\n!keep.log\r\n"}, "keep.log", False), ({"": "a \r\n"}, "a", True), ({"": "\ufeff*.log\n"}, "a.log", True),
    ({"": "\ufeff*.log\r\n#c\r\n"}, "x/b.log", True), ({"": "\ufeff#*.log\n"}, "a.log", False),
    ({"": "a\\*b\n"}, "a*b", True), ({"": "a\\*b\n"}, "aXb", False), ({"": "**/**/x\n"}, "x", True),
    ({"": "a/**/**/b\n"}, "a/p/q/b", True), ({"": "a/**/**\n"}, "a/p", True), ({"": "a/**/**\n"}, "a", False),
    ({"": "*.py[co]\n"}, "m.pyc", True), ({"": "[[:digit:]]x\n"}, "5x", True), ({"": "\\!bang\n"}, "!bang", True),
]


def _git_says(tmp_path: Path, files: dict[str, str], path: str) -> bool:
    repo = tmp_path / "oracle"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, capture_output=True)
    (repo / ".git" / "info" / "exclude").write_text("", encoding="utf-8")
    for directory, text in files.items():
        target = repo / directory / ".gitignore"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    leaf = repo / path
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text("x\n", encoding="utf-8")
    done = subprocess.run(
        ["git", "-c", "core.excludesFile=/dev/null", "-c", "core.ignorecase=false", "check-ignore", "--no-index", "-q", "--", path],
        cwd=repo, capture_output=True, text=True,
    )
    assert done.returncode in (0, 1), done.stderr
    return done.returncode == 0


@pytest.mark.parametrize(("files", "path", "expected"), CASES)
def test_the_matcher_follows_git(files, path, expected, tmp_path):
    assert gi.Ignore(files).ignored(path) is expected
    assert _git_says(tmp_path, files, path) is expected


def test_a_file_over_the_size_cap_contributes_nothing_and_one_note():
    big = "*.log\n" + "#" * (gi.MAX_FILE_BYTES + 10)
    ignore = gi.Ignore({"": big})
    assert ignore.ignored("a.log") is False
    assert len(ignore.notes) == 1


def test_a_file_over_the_line_cap_contributes_nothing_and_one_note():
    ignore = gi.Ignore({"": "*.log\n" * (gi.MAX_LINES + 1)})
    assert ignore.ignored("a.log") is False
    assert len(ignore.notes) == 1


def test_a_long_pattern_is_skipped_with_a_note_and_the_others_still_apply():
    ignore = gi.Ignore({"": "a" * 600 + "\n*.log\n"})
    assert ignore.ignored("x.log") is True
    assert ignore.ignored("a" * 200) is False
    assert len(ignore.notes) == 1


def test_a_clean_file_leaves_no_note():
    assert gi.Ignore({"": "*.log\n"}).notes == []


def test_many_stars_do_not_backtrack_badly():
    ignore = gi.Ignore({"": "a*a*a*a*a*a*a*a*b\n"})
    start = time.monotonic()
    ignore.ignored("a" * 5000)  # a name this long is judged ignored, without a walk
    assert ignore.ignored("a" * gi.MAX_NAME) is False
    assert time.monotonic() - start < 1.0


def test_a_path_too_deep_or_with_a_name_too_long_is_judged_ignored_with_one_note():
    ignore = gi.Ignore({})
    assert ignore.ignored("/".join(["d"] * (gi.MAX_DEPTH + 1))) is True
    assert ignore.ignored("a" * (gi.MAX_NAME + 1)) is True
    assert ignore.ignored("/".join(["d"] * gi.MAX_DEPTH)) is False
    assert len(ignore.notes) == 1


def test_a_backtracking_segment_is_cheap_to_ask_and_correct():
    ignore = gi.Ignore({"": "*a*[bc]*d\n"})
    assert ignore.ignored("xaybzd") is True
    assert ignore.ignored("xaydzd") is False
    heavy = gi.Ignore({"": "".join(f"*a*[bc]*d{n}\n" for n in range(100))})
    assert len(heavy.notes) == 1  # past the budget for segments that can backtrack


def test_many_double_stars_and_a_deep_path_stay_fast():
    ignore = gi.Ignore({"": "/".join(["**"] * 40) + "/z\n"})
    start = time.monotonic()
    assert ignore.ignored("/".join(["d"] * 60) + "/y") is False
    assert time.monotonic() - start < 1.0


def _timed(files, paths):
    ignore = gi.Ignore(files)
    start = time.monotonic()
    for path in paths:
        ignore.ignored(path)
    return time.monotonic() - start, ignore


def test_many_hostile_rules_within_the_caps_judge_a_thousand_paths_quickly():
    paths = [f"pkg{n % 7}/mod{n}/sub/file{n}.py" for n in range(1000)]
    wide = ["/".join(["a" * 200] * 12) + f"/{'a' * 40}{n:010d}" for n in range(1000)]
    deep = ["/".join(["x"] * 60) + f"/f{n}" for n in range(1000)]
    hostile = {
        "doublestars": "\n".join("**/" * 40 + f"lit{n}" for n in range(1900)) + "\n",
        "stars": "\n".join("**/" * 20 + "a*" * 8 + f"b{n}" for n in range(3000)) + "\n",
        "stars_and_marks": "\n".join("**/" * 20 + "*a?" * 8 + f"b{n}" for n in range(2700)) + "\n",
        "classes": "\n".join("*[ab]" * 8 + f"[x{n % 9}]" for n in range(3000)) + "\n",
        "middle": "\n".join("x/" + "**/x/" * 7 + f"*b{n}" for n in range(3000)) + "\n",
        "spread": "\n".join("**/x/" * 7 + "**/*" for _ in range(3000)) + "\n",
    }
    for name, text in hostile.items():
        assert len(text.encode()) <= gi.MAX_FILE_BYTES, name
        for sample in (paths, wide, deep):
            took, _ = _timed({"": text}, sample)
            assert took < 2.0, (name, took)


def test_an_honest_file_keeps_every_pattern_and_stays_fast():
    text = "".join(f"/dir{n}/\n*.ext{n}\n" for n in range(300)) + "build/\n"
    took, ignore = _timed({"": text}, [f"pkg/mod{n}/file{n}.py" for n in range(1000)])
    assert ignore.notes == [] and took < 1.0
    assert ignore.ignored("x/build/a") and ignore.ignored("dir299/a.py") and ignore.ignored("q/z.ext7")


def test_a_pattern_past_the_limits_is_left_out_with_one_note():
    text = "/".join(["a"] * (gi.MAX_SEGMENTS + 1)) + "\n" + "*" * 3 + "a*" * (gi.MAX_STARS + 1) + "\n*.log\n"
    ignore = gi.Ignore({"": text})
    assert ignore.ignored("x.log") is True
    assert ignore.ignored("/".join(["a"] * (gi.MAX_SEGMENTS + 1))) is False
    assert len(ignore.notes) == 1


def test_the_token_budget_leaves_out_the_rest_with_one_note():
    ignore = gi.Ignore({"": "".join(f"*a*b*c{n}\n" for n in range(2000))})
    assert len(ignore.notes) == 1
    assert ignore.ignored("xaybzc0") is True
    assert ignore.ignored("xaybzc1999") is False


def test_an_unclosed_class_is_a_literal_bracket():
    ignore = gi.Ignore({"": "a[b\n"})
    assert ignore.ignored("a[b") is True
    assert ignore.ignored("ab") is False


def test_blank_lines_comments_and_a_lone_slash_are_skipped():
    ignore = gi.Ignore({"": "\n   \n# c\n/\n"})
    assert ignore.ignored("c") is False and ignore.ignored("anything") is False
    assert ignore.notes == []


def test_exclude_patterns_are_passed_as_a_root_file():
    exclude = gi.Ignore({"": "\n".join(["generated/", "*.snap"])})
    assert exclude.ignored("a/generated/x.py") is True
    assert exclude.ignored("t/x.snap") is True
    assert exclude.ignored("t/x.py") is False
