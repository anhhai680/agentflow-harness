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
    assert ignore.ignored("a" * 600) is False
    assert len(ignore.notes) == 1


def test_a_clean_file_leaves_no_note():
    assert gi.Ignore({"": "*.log\n"}).notes == []


def test_many_stars_do_not_backtrack_badly():
    ignore = gi.Ignore({"": "a*a*a*a*a*a*a*a*b\n"})
    start = time.monotonic()
    assert ignore.ignored("a" * 5000) is False
    assert time.monotonic() - start < 1.0


def test_many_double_stars_and_a_deep_path_stay_fast():
    ignore = gi.Ignore({"": "/".join(["**"] * 40) + "/z\n"})
    start = time.monotonic()
    assert ignore.ignored("/".join(["d"] * 400) + "/y") is False
    assert time.monotonic() - start < 1.0


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
