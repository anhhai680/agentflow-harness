"""Parsing and validating backlog.yml: the file-level and item-level errors, and the safe read."""

from __future__ import annotations

import pytest
import yaml
from agentflow import backlog_file
from agentflow.backlog_file import BacklogItem, Problem

GOOD = "version: 1\nitems:\n  - id: a\n    title: A\n    status: ready\n  - id: b\n    title: B\n    depends_on: [a]\n    issue: 7\n"


def test_parse_reads_every_key_with_defaults():
    items, problems = backlog_file.parse(GOOD)
    assert problems == []
    assert items == [
        BacklogItem("a", [], title="A", status="ready"),
        BacklogItem("b", ["a"], title="B", status="backlog", issue=7),
    ]


def test_parse_accepts_a_bom_crlf_and_comments():
    text = "﻿# top\r\n" + GOOD.replace("\n", "\r\n").replace("  - id: b", "  # between\r\n  - id: b")
    assert [i.id for i in backlog_file.parse(text)[0]] == ["a", "b"]


@pytest.mark.parametrize(
    "text,where",
    [
        ("- a\n", "backlog.yml"),
        ("version: 2\nitems: []\n", "version"),
        ("items: []\n", "version"),
        ("version: true\nitems: []\n", "version"),
        ("version: 1\nitems: {}\n", "items"),
        ("{{{", "backlog.yml"),
        ("version: 1\nitems:\n" + "  - {id: x, title: X}\n" * 2001, "items"),
    ],
)
def test_a_broken_file_has_no_items_and_one_error(text, where):
    items, problems = backlog_file.parse(text)
    assert items == [] and [(p.level, p.where) for p in problems] == [("error", where)]


@pytest.mark.parametrize(
    "item,where",
    [
        ({"id": "Bad Id", "title": "t"}, "items[0].id"),
        ({"id": "a" * 65, "title": "t"}, "items[0].id"),
        ({"id": 5, "title": "t"}, "items[0].id"),
        ({"id": "a"}, "items[0].title"),
        ({"id": "a", "title": ""}, "items[0].title"),
        ({"id": "a", "title": "x" * 201}, "items[0].title"),
        ({"id": "a", "title": "two\nlines"}, "items[0].title"),
        ({"id": "a", "title": "t", "status": "wip"}, "items[0].status"),
        ({"id": "a", "title": "t", "issue": 0}, "items[0].issue"),
        ({"id": "a", "title": "t", "issue": True}, "items[0].issue"),
        ({"id": "a", "title": "t", "issue": "7"}, "items[0].issue"),
        ({"id": "a", "title": "t", "notes": "x" * 4097}, "items[0].notes"),
        ({"id": "a", "title": "t", "lane": 3}, "items[0].lane"),
        ({"id": "a", "title": "t", "depends_on": [1]}, "items[0].depends_on"),
        ({"id": "a", "title": "t", "depends_on": {"x": 1}}, "items[0].depends_on"),
        ("just a string", "items[0]"),
    ],
)
def test_a_bad_item_is_dropped_with_an_error(item, where):
    items, problems = backlog_file.parse(yaml.safe_dump({"version": 1, "items": [item]}))
    assert items == [] and [(p.level, p.where) for p in problems] == [("error", where)]


def test_a_bad_item_leaves_its_neighbours():
    text = yaml.safe_dump({"version": 1, "items": [{"id": "a", "title": "A"}, {"id": "B!", "title": "t"}, {"id": "c", "title": "C"}]})
    items, problems = backlog_file.parse(text)
    assert [i.id for i in items] == ["a", "c"]
    assert [p.where for p in problems] == ["items[1].id"]


def test_depends_on_takes_a_comma_separated_string():
    text = yaml.safe_dump({"version": 1, "items": [{"id": "a", "title": "A", "depends_on": "x, y"}]})
    assert backlog_file.parse(text)[0][0].deps == ["x", "y"]


@pytest.mark.parametrize("key", ["spec", "plan"])
@pytest.mark.parametrize("path", ["/etc/passwd", "../x.md", "docs/../../x.md"])
def test_a_path_leaving_the_repository_is_cleared_with_an_error(key, path):
    items, problems = backlog_file.parse(yaml.safe_dump({"version": 1, "items": [{"id": "a", "title": "t", key: path}]}))
    assert getattr(items[0], key) == "" and problems[0].where == f"items[0].{key}"


def test_a_relative_path_is_kept():
    text = yaml.safe_dump({"version": 1, "items": [{"id": "a", "title": "t", "spec": "docs/specs/a.md"}]})
    items, problems = backlog_file.parse(text)
    assert items[0].spec == "docs/specs/a.md" and problems == []


def test_a_duplicate_id_keeps_the_first_and_names_both_positions():
    items, problems = backlog_file.parse(GOOD + "  - id: a\n    title: again\n")
    assert [i.title for i in items] == ["A", "B"]
    assert problems == [Problem("error", "items[2].id", "duplicate id a (first at items[0])")]


def test_an_unknown_key_is_a_warning_and_the_item_stays():
    items, problems = backlog_file.parse(GOOD.replace("    issue: 7\n", "    owner: me\n"))
    assert len(items) == 2 and [(p.level, p.where) for p in problems] == [("warning", "items[1].owner")]


def test_read_a_missing_file(tmp_path):
    items, problems = backlog_file.read(tmp_path / "backlog.yml")
    assert items == [] and problems == [Problem("error", "backlog.yml", "missing: agentflow backlog import writes it")]


def test_read_a_good_file(tmp_path):
    path = tmp_path / "backlog.yml"
    path.write_text(GOOD, encoding="utf-8")
    items, problems = backlog_file.read(path)
    assert [i.id for i in items] == ["a", "b"] and problems == []


def test_read_refuses_a_symlink_and_an_oversize_file(tmp_path):
    real = tmp_path / "real.yml"
    real.write_text(GOOD, encoding="utf-8")
    link = tmp_path / "backlog.yml"
    link.symlink_to(real)
    items, problems = backlog_file.read(link)
    assert items == [] and [p.level for p in problems] == ["error"]

    big = tmp_path / "big.yml"
    big.write_text("version: 1\nitems: []\n# " + "x" * backlog_file.FILE_MAX, encoding="utf-8")
    items, problems = backlog_file.read(big)
    assert items == [] and [p.level for p in problems] == ["error"]


def test_read_a_file_that_is_not_utf8(tmp_path):
    path = tmp_path / "backlog.yml"
    path.write_bytes(b"version: 1\nitems: []\n# \xff\xfe\n")
    items, problems = backlog_file.read(path)
    assert items == [] and [p.level for p in problems] == ["error"]
