"""graph_ignore: a pure .gitignore matcher, cross-checked against `git check-ignore` itself."""

from __future__ import annotations

import subprocess
import sys
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
    # an empty segment names nothing in git
    ({"": "a//b\n"}, "a/b", False), ({"": "c//\n"}, "c/x", False), ({"": "c//\n"}, "c", False), ({"": "//d\n"}, "d", False),
    ({"": "e/\n"}, "e/x", True), ({"": "*.py[co]\n"}, "m.pyc", True), ({"": "[[:digit:]]x\n"}, "5x", True), ({"": "\\!bang\n"}, "!bang", True),
    # an unclosed `[` is never a class, and the rules after it still apply
    ({"": "[\n*.log\n"}, "a.log", True), ({"": "a[b\n"}, "ab", False), ({"": "x[[:\n"}, "x", False),
    ({"": "[[:\n*.log\n"}, "b.log", True), ({"": "[a-\n"}, "a", False), ({"": "x[a-\n*.log\n"}, "x.log", True),
    ({"": "[!x\n"}, "y", False), ({"": "[\\]x\n"}, "]x", False),
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
    assert ignore.ignored("a" * 5000) is False
    assert time.monotonic() - start < 1.0


def test_a_backtracking_segment_is_correct():
    ignore = gi.Ignore({"": "*a*[bc]*d\n"})
    assert ignore.ignored("xaybzd") is True
    assert ignore.ignored("xaydzd") is False


def _steps(files, paths):
    ignore = gi.Ignore(files)
    return ignore, [ignore.judge(path)[1] for path in paths]


def _deep(depth, n=40):
    return [f"top{i % 3}/mid{i}/" + "/".join(f"d{k}" for k in range(depth - 2)) + f"/file{i}.py" for i in range(n)]


def _mixed(lines):
    out = []
    for n in range(lines // 10):
        out += [f"*.ext{n}", f"/dir{n}/", f"build{n}/", f"**/cache{n}", "*.py[cod]", f"[Tt]emp{n}*", f"logs/**/*.log{n}",
                f"!keep{n}.log", f"pkg{n}/*/gen", f"# comment {n}"]
    return "\n".join(out)


HONEST = {
    "1000 wild rules at depth 20": ({"": "".join(f"*.ext{n}\n" for n in range(1000))}, _deep(20)),
    "5000 mixed lines at depth 20": ({"": _mixed(4990)}, _deep(20)),
    "this repository's own file": ({"": (Path(__file__).parents[2] / ".gitignore").read_text(encoding="utf-8")}, _deep(20)),
}


@pytest.mark.parametrize("name", sorted(HONEST))
def test_honest_files_stay_under_a_quarter_of_the_step_budget(name):
    files, paths = HONEST[name]
    ignore, steps = _steps(files, paths)
    assert ignore.notes == []
    assert max(steps) * 4 <= gi.MAX_STEPS, (name, max(steps))


REVIEWED_SHAPES = {
    "plain": "**/x/" * 7 + "**/*.py",
    "class": "**/[x]/" * 7 + "**/*.py",
    "star": "**/x*/" * 7 + "**/*.py",
    "mark": "**/?/" * 7 + "**/*.py",
}
_K = 8
WALK = "\n".join("**/" + "[a]*" * (_K - 1) + "[b]/**/" + "[a]*" * _K for _ in range(400)) + "\n"
HOSTILE = {
    **{f"{shape}-{depth}": ("\n".join(REVIEWED_SHAPES[shape] for _ in range(3000)) + "\n", depth)
       for shape in REVIEWED_SHAPES for depth in (62, 10)},
    "walk": (WALK, 62),
}


def _hostile_paths(name, depth, n):
    if name == "walk":  # fresh directories, then segments the walk can spend its steps on
        return [f"q{i}/" + "/".join(["a" * _K] * (depth - 1)) + "/f" for i in range(n)]
    return ["/".join(["x"] * depth) + f"/f{i}.py" for i in range(n)]


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_a_hostile_file_costs_at_most_the_step_budget_per_path(name):
    text, depth = HOSTILE[name]
    assert len(text.encode()) <= gi.MAX_FILE_BYTES
    ignore, steps = _steps({"": text}, _hostile_paths(name, depth, 8))
    assert max(steps) <= gi.MAX_STEPS


def test_a_hostile_file_cannot_hang_a_build():
    start = time.monotonic()
    ignore, steps = _steps({"": WALK}, _hostile_paths("walk", 62, 100))
    assert time.monotonic() - start < 30.0
    assert ignore.notes and max(steps) == gi.MAX_STEPS


def test_the_same_rules_in_sixty_nested_directories_stay_bounded():
    text = "\n".join(REVIEWED_SHAPES["plain"] for _ in range(3000)) + "\n"
    files = {"/".join(["x"] * depth): text for depth in range(60)}
    ignore, steps = _steps(files, ["/".join(["x"] * 60) + f"/f{n}.py" for n in range(20)])
    assert max(steps) <= gi.MAX_STEPS


def test_a_call_that_runs_out_is_ignored_with_one_note_and_the_answer_does_not_depend_on_the_order(monkeypatch):
    files = {"": "".join(f"*.ext{n}\n" for n in range(1000))}
    paths = _deep(20, 6)
    fresh = gi.Ignore(files).judge(paths[0])
    assert fresh[1] < gi.MAX_STEPS  # sanity: the file alone fits
    monkeypatch.setattr(gi, "MAX_STEPS", 15_000)  # a path 20 deep in fresh directories no longer fits
    a, b = gi.Ignore(files), gi.Ignore(files)
    forward = [a.judge(path) for path in paths]
    backward = [b.judge(path) for path in reversed(paths)][::-1]
    assert forward == backward
    assert forward[0] == (True, 15_000) and a.notes == b.notes and len(a.notes) == 1
    assert [a.ignored(path) for path in paths] == [b.ignored(path) for path in paths] == [True] * len(paths)


def test_an_honest_file_keeps_every_pattern_and_stays_correct():
    text = "".join(f"/dir{n}/\n*.ext{n}\n" for n in range(300)) + "build/\n"
    ignore = gi.Ignore({"": text})
    assert ignore.notes == []
    assert ignore.ignored("x/build/a") and ignore.ignored("dir299/a.py") and ignore.ignored("q/z.ext7")
    assert not ignore.ignored("pkg/mod/file.py")


UNCLOSED = {"open": "[", "posix": "[[:", "range": "[a-", "escaped": "[\\]", "negated": "[!"}  # units of a line


@pytest.mark.parametrize("name", sorted(UNCLOSED))
def test_a_file_of_unclosed_classes_parses_in_linear_time(name):
    line = UNCLOSED[name] * (511 // len(UNCLOSED[name]))
    text = "\n".join([line] * (gi.MAX_FILE_BYTES // (len(line) + 1)))
    assert len(text.encode()) <= gi.MAX_FILE_BYTES and len(line) <= gi.MAX_PATTERN
    start = time.monotonic()
    ignore = gi.Ignore({"": text})
    assert time.monotonic() - start < 5.0  # a hang detector: it takes about 0.1 s, and took up to 13 s when quadratic
    assert ignore.notes == []


def _lines_run(text: str) -> int:
    """How many lines of graph_ignore building an Ignore from `text` runs: a count that does not depend on the machine."""
    count = 0

    def local(frame, event, arg):
        nonlocal count
        count += event == "line"
        return local

    def calls(frame, event, arg):
        return local(frame, event, arg) if frame.f_code.co_filename == gi.__file__ else None

    previous = sys.gettrace()
    sys.settrace(calls)
    try:
        gi.Ignore({"": text})
    finally:
        sys.settrace(previous)
    return count


@pytest.mark.parametrize("name", sorted(UNCLOSED))
def test_parsing_unclosed_classes_grows_linearly_with_the_line(name):
    unit = UNCLOSED[name]
    short, long = _lines_run(unit * (120 // len(unit))), _lines_run(unit * (480 // len(unit)))
    assert long < 6 * short  # four times the line costs about four times the work (it was about 15 times)


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
