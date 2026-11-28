"""graph_select: the state of each tracked path and the language to parse it as."""

from __future__ import annotations

import pytest

from agentflow import graph_select as gs
from agentflow.graph_ignore import Ignore

NONE = Ignore({})


def _state(path, mode="100644", ignore=NONE, exclude=NONE):
    return gs.state(path, mode, ignore=ignore, exclude=exclude)


def test_the_states_are_the_documented_ones():
    assert gs.STATES == ("indexed", "unparsed", "ignored", "secret", "vendored", "excluded", "symlink", "submodule")


def test_a_symlink_named_like_a_secret_is_a_symlink():
    assert _state(".env", "120000") == "symlink"


def test_a_submodule_is_a_submodule_before_anything_else():
    assert _state("node_modules/x", "160000", ignore=Ignore({"": "*\n"})) == "submodule"


@pytest.mark.parametrize(
    "path",
    [".ENV.local", ".env", "a/.env.production", "k.pem", "a/B.KEY", "x.p12", "x.pfx", "x.keystore", "id_rsa", "id_rsa.pub",
     "id_ed25519", "ID_ED25519.pub", "config/credentials.json", "aws_Credentials"],
)
def test_secret_files(path):
    assert _state(path) == "secret"


def test_a_secret_is_secret_even_when_ignored():
    assert _state("a/.env", ignore=Ignore({"": ".env\n"})) == "secret"


@pytest.mark.parametrize("path", ["environment.py", "keyboard.py", "a/envfile", "pem.py", "rsa_id"])
def test_near_misses_are_not_secrets(path):
    assert _state(path) == "indexed"


def test_ignored_comes_before_vendored():
    assert _state("x/node_modules/a.js", ignore=Ignore({"": "node_modules/\n"})) == "ignored"


@pytest.mark.parametrize(
    "path",
    ["x/node_modules/a.js", "vendor/m.go", "a/third_party/b.py", "bower_components/x.js", "l/site-packages/p.py", ".venv/a.py",
     "venv/b.py", "p/__pycache__/m.pyc", "a.min.js", "a/b.min.css", "a.js.map", "x_pb2.py", "x_pb2_grpc.py", "uv.lock",
     "package-lock.json", "pnpm-lock.yaml"],
)
def test_vendored_paths(path):
    assert _state(path) == "vendored"


def test_vendored_comes_before_excluded():
    assert _state("vendor/a.py", exclude=Ignore({"": "vendor/\n"})) == "vendored"


def test_an_exclude_hit_is_excluded():
    assert _state("gen/a.py", exclude=Ignore({"": "gen/\n"})) == "excluded"


def test_a_plain_path_is_indexed():
    assert _state("pkg/a.py") == "indexed"


@pytest.mark.parametrize(
    ("path", "lang"),
    [("a.py", "python"), ("a.ts", "typescript"), ("a.mts", "typescript"), ("a.cts", "typescript"), ("a.tsx", "tsx"),
     ("a.js", "javascript"), ("a.mjs", "javascript"), ("a.cjs", "javascript"), ("a.jsx", "javascript"),
     ("a.md", "markdown"), ("pyproject.toml", "manifest"), ("x/package.json", "manifest"),
     ("requirements.txt", "manifest"), ("req/requirements-dev.txt", "manifest"), ("a.txt", None), ("a.json", None),
     ("Makefile", None), ("a.go", None), ("pyproject.toml.bak", None)],
)
def test_language(path, lang):
    assert gs.language(path) == lang


@pytest.mark.parametrize(
    ("path", "expected"),
    [("test_a.py", True), ("x/test_a.py", True), ("a_test.py", True), ("a.test.ts", True), ("a.spec.tsx", True),
     ("a.test.js", True), ("a.spec.mjs", True), ("a.test.py", False), ("a.py", False), ("tests/a.py", True),
     ("src_x/__tests__/a.js", True), ("pkg/testing/a.py", False), ("a.spec.md", False), ("contest.py", False)],
)
def test_is_test(path, expected):
    assert gs.is_test(path) is expected


def test_parses_markdown_only_in_doc_roots_or_at_the_top():
    assert gs.parses("docs/a.md", ["docs"]) == "markdown"
    assert gs.parses("pkg/notes.md", ["docs"]) is None
    assert gs.parses("README.md", []) == "markdown"
    assert gs.parses("docsx/a.md", ["docs"]) is None
    assert gs.parses("docs/sub/a.md", ["docs/"]) == "markdown"


def test_parses_other_languages_anywhere():
    assert gs.parses("a/b/c.py", []) == "python"
    assert gs.parses("a/notes.txt", ["docs"]) is None
