"""Which tracked paths the knowledge graph indexes, and the language each one is parsed as.

`state` gives every tracked path one verdict from its git mode, its name and the ignore patterns, and only an
`indexed` path may get nodes. The order is fixed and the first hit wins: a symlink or a submodule is never read, a
secret-looking name is never read even when force-added, then the repository's `.gitignore` files, then the fixed list
of vendored and generated paths, then the pack's `graph.exclude`. `unparsed` is not decided here: it is the state of
an indexed path whose language is unknown or whose parse failed. Pure: no file, no clock, no subprocess.
"""

from __future__ import annotations

from collections.abc import Sequence

from agentflow.graph_ignore import Ignore

STATES = ("indexed", "unparsed", "ignored", "secret", "vendored", "excluded", "symlink", "submodule")

_SYMLINK, _SUBMODULE = "120000", "160000"
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")
_SECRET_PREFIXES = ("id_rsa", "id_ed25519")
_VENDORED_SEGMENTS = frozenset(
    {"node_modules", "vendor", "third_party", "bower_components", "site-packages", ".venv", "venv", "__pycache__"}
)
_VENDORED_SUFFIXES = (".min.js", ".min.css", ".map", "_pb2.py", "_pb2_grpc.py", ".lock")
_VENDORED_NAMES = frozenset({"package-lock.json", "pnpm-lock.yaml"})
_LANGUAGES = {
    ".py": "python", ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript", ".md": "markdown",
}
_SCRIPT_EXTENSIONS = frozenset({".ts", ".mts", ".cts", ".tsx", ".js", ".mjs", ".cjs", ".jsx"})
_TEST_DIRS = frozenset({"tests", "__tests__"})


def _basename(path: str) -> str:
    return path.rpartition("/")[2]


def _extension(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:] if dot > 0 else ""


def language(path: str) -> str | None:
    """The language a path is parsed as, from its name, or None."""
    name = _basename(path)
    if name in ("pyproject.toml", "package.json") or (name.startswith("requirements") and name.endswith(".txt")):
        return "manifest"
    return _LANGUAGES.get(_extension(name))


def is_test(path: str) -> bool:
    """Whether the path names a test file or lies in a tests directory."""
    *directories, name = path.split("/")
    if any(part in _TEST_DIRS for part in directories) or name in _TEST_DIRS:
        return True
    if name.startswith("test_") and name.endswith(".py") or name.endswith("_test.py"):
        return True
    ext = _extension(name)
    stem = name[: len(name) - len(ext)]
    return ext in _SCRIPT_EXTENSIONS and stem.endswith((".test", ".spec"))


def _secret(name: str) -> bool:
    low = name.lower()
    return (
        low == ".env"
        or low.startswith(".env.")
        or low.endswith(_SECRET_SUFFIXES)
        or low.startswith(_SECRET_PREFIXES)
        or "credentials" in low
    )


def _vendored(path: str) -> bool:
    name = _basename(path)
    return (
        any(part in _VENDORED_SEGMENTS for part in path.split("/"))
        or name.endswith(_VENDORED_SUFFIXES)
        or name in _VENDORED_NAMES
    )


def state(path: str, mode: str, *, ignore: Ignore, exclude: Ignore) -> str:
    """The first of symlink, submodule, secret, ignored, vendored, excluded that holds, else `indexed`."""
    if mode == _SYMLINK:
        return "symlink"
    if mode == _SUBMODULE:
        return "submodule"
    if _secret(_basename(path)):
        return "secret"
    if ignore.ignored(path):
        return "ignored"
    if _vendored(path):
        return "vendored"
    if exclude.ignored(path):
        return "excluded"
    return "indexed"


def parses(path: str, doc_roots: Sequence[str]) -> str | None:
    """The language to parse the path as, or None for a file node only. Markdown is read at the top or under a doc root."""
    lang = language(path)
    if lang != "markdown":
        return lang
    if "/" not in path:
        return lang
    roots = [root.strip("/") for root in doc_roots]
    return lang if any(root and path.startswith(root + "/") for root in roots) else None
