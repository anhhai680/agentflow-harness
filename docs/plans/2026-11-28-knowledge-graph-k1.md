# Knowledge graph, slice K1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** With `graph.enabled: true` in its pack, a developer runs `agentflow graph build` in one repository and then `agentflow graph status|query|show`, over an index of its Python, TypeScript, JavaScript, Markdown and manifest files that is rebuilt only from what changed.

**Architecture:** Pure rule modules do the thinking: `graph_ignore` and `graph_select` decide which tracked paths are indexed, one extractor per source turns a file's bytes into *facts*, and `graph_resolve` turns facts into nodes and edges. All parsing of repository bytes happens in a child process (`graph_worker.py`, a locked `uv` script) that `graph_parse` starts, limits and distrusts. `graph_git` reads commits through hardened git, `graph_store` keeps one SQLite file per repository as a snapshot that is replaced by rename, and `graph_build` ties them into a delta build. No prompt, lane or run changes.

**Tech Stack:** Python 3.12, stdlib (`ast`, `sqlite3` with FTS5, `tomllib`, `json`, `resource`) + `pyyaml` in the engine; `tree-sitter` 0.27.0 with the JavaScript and TypeScript grammars in the worker only; pytest with real temp git repositories.

**Spec:** `docs/specs/agentflow-knowledge-graph-proposal.md` (approved 2026-11-28), slice **K1**: Design sections 1, 2, 3 and 9, the Security section, decisions 2, 9 to 12, 14, 19, 20, 26 and 27, the amendments of 2026-11-28, and success criteria 1, 2, 3, 8 and 9 (for three of its six languages).

### What K1 does not do

- No `{context}` slot, no build at `pipeline.start`, no `graph.build_timeout_s`, no run or event changes (K2).
- No C#, Java or Go, no XML manifests, no `coarse` extractor (K1b). A TypeScript or JavaScript file whose grammar is not installed gets a file node and a note.
- No other repository: no linker, no `workspace.graph`, no `graph.share`, no `--all`, no `graph impact` (K3).
- No run logs, backlog or lessons in the index, no `graph why`, no glossary `term` nodes (K2).
- No diagrams, dashboard tab, `ask` or MCP server (K6 to K8).
- No YAML manifests (compose files, workflow `uses:`): K1 parses no YAML, so the worker needs no `pyyaml`.

### Plan decisions

- **`agentflow graph build` needs `graph.enabled: true`.** With it off the command prints one line that names the key and exits 1. `status`, `query` and `show` work on whatever index exists.
- **Facts, then resolution.** An extractor sees one file and returns `Facts` (definitions, imports, names referred to, sections, links, dependencies). Facts are cached by blob id. Edges are made afterwards by `graph_resolve` from stored facts, with no parse. This is what makes decision 26 hold: a file affected by another file's change is re-resolved and never re-read.
- **Which files a change affects** is decided by keys. Every file *provides* keys (its path, its module names) and *wants* keys (what its imports and links could resolve to). After a change, the files re-resolved are the changed ones plus every file that wants a key a changed file provided before or provides now.
- **Every tracked path gets a row** in the store's `files` table, with a state (`indexed`, `unparsed`, `ignored`, `secret`, `vendored`, `excluded`, `symlink`, `submodule`). Only `indexed` and `unparsed` rows have nodes. That is how `graph status` counts what was left out and `graph show <path>` says why a path has no node. A left-out path is never opened.
- **`.gitignore` texts are kept in the store** (`ignore_files`), so a delta that changes no `.gitignore` judges its changed paths without listing the tree.
- **`graph.exclude` uses `.gitignore` syntax,** parsed by the same matcher, so there is one pattern language.
- **Markdown is indexed at the repository root and under the folders `project.docs` names.** Other `.md` files get a file node only.
- **A node carries its file's blob id, not a commit.** The snapshot's commit is in the store's `meta`, and every output prints it. A node unchanged since an older commit is still exactly what the snapshot's commit holds.
- **Doctor runs no new command.** It checks FTS5 in-process and reads the last build's result from the store.
- **The worker pins only what K1 parses:** `tree-sitter==0.27.0`, `tree-sitter-javascript==0.25.0`, `tree-sitter-typescript==0.23.2`. K1b adds the other three grammars.
- **Names are ASCII.** An identifier outside the allowed characters is dropped and counted in a note, not escaped (the spec's rule for what may reach a prompt later).

## Threat model

| Boundary | Untrusted value | Threat | Control, and the task that tests it |
|---|---|---|---|
| Repository bytes into a parser | file contents | a crash, a hang, memory exhaustion, code execution through a parser bug | parse in a child process; 10 s per file; `RLIMIT_AS` on Linux and a resident-size watchdog elsewhere; bounded restarts (Task 6). Code execution is a stated residual risk: the worker is not an OS sandbox. |
| Worker output into the engine | the JSON it returns | forged or oversized facts, names carrying text | a reply size cap, `graph_model.check_facts`, ASCII name sets, count caps (Tasks 2, 6) |
| Git metadata into SQL, the terminal and paths | paths, ref names | SQL injection, terminal escape sequences, option injection into git | bound parameters only; `doctor.printable`; ids checked as hex; `--end-of-options`; no path ever opened on disk (Tasks 9 to 11) |
| A person's query into FTS | the query text | an FTS syntax error or injected operator | tokens of `[a-z0-9]` only, each quoted, bound as one parameter (Task 9) |
| PyPI into the worker | three packages | a tampered or swapped package | exact pins, a hash lock, `--locked` always, download only on an explicit `graph build` (Task 7) |
| The repository's own `.gitignore` | patterns | catastrophic matching, a giant file | size, line and pattern caps; a translation with no nested repetition (Task 3) |
| The store on disk | the SQLite file | another local process reads indexed doc text | directory `0700`, file `0600`; nothing but names, signatures, one doc line and doc sections is stored (Task 9) |

Abuse cases that are tests: a planted `conftest.py`, `setup.py` and `sitecustomize.py` never run (Task 10); a force-added `.env` and a force-added ignored file are never opened (Tasks 3, 10); a file that hangs the parser costs one note (Task 6); a worker that answers with a node for another path is refused (Task 6).

## Global Constraints

- The engine process imports only the stdlib and `pyyaml>=6`. `tree_sitter*` is imported only inside `agentflow/graph_syntax.py`, and only in the worker process.
- The modules the worker imports (`graph_model`, `graph_python`, `graph_markdown`, `graph_manifest`, `graph_syntax`) import nothing from the engine but `graph_model`, and never `yaml`.
- No project strings in the engine (`test_portability.DENYLIST`). It matches `src/`, `docs/specs`, `SPEC-` and `next-step`, so no engine file may contain those, in a pattern list or a comment.
- The golden `explain` files stay byte-identical. With `graph.enabled: false` (the default) every existing test passes unchanged.
- Nothing in an indexed repository is executed, imported or evaluated. The working tree is never read: every byte comes from a git object at a named commit.
- A path that the repository's `.gitignore` names, or that matches a secret-file pattern, is never opened and never reaches the worker.
- A build makes no call to a git remote (`GIT_NO_LAZY_FETCH=1`, `GIT_TERMINAL_PROMPT=0` on every git call). Only `agentflow graph build` may download the worker's packages.
- Past the first build, a build parses only blobs that changed, and no unchanged file is opened.
- Every git call uses `guards.HARDENED` and `--no-pager`; every SQL value is a bound parameter; every name or path printed goes through `doctor.printable`.
- Each new config key ships with its Settings Form field, its `validate` check, a `test_config_edit.py` case and a playbook §14 line.
- `uvx ruff@0.17.4 check .` must print `All checks passed!` before every commit. Tests run with `uv run --no-project --with pyyaml --with pytest pytest <file> --import-mode=prepend -q`.
- A commit message is one imperative sentence ending with the pack's `project.commit_trailer`.

## Review Focus

1. **A path the terminal or the index must not trust**: a tracked file whose name holds a newline, an escape sequence or bytes that are not UTF-8. It is left out with a note and never printed raw (Tasks 10, 11).
2. **A worker that misbehaves instead of crashing**: half a reply and then silence, a reply of 100 MB on one line, a reply that is valid JSON for a different file. Each is one note for that file, the worker is restarted, and the build finishes (Task 6).
3. **A build that is interrupted or doubled**: killed between the delta and the rename, or started while another build runs. The old snapshot is whole and readable, the leftover temp file is removed by the next build, and the second build refuses with one line (Task 10).
4. **A query that is all punctuation or FTS syntax** (`"* OR NEAR(`, an empty string, 5,000 characters). It returns no match and no error (Task 9).
5. **A store this engine cannot use**: a file from a newer schema, a truncated file, a directory where the file should be. Reads report "no usable index" and a build starts a fresh one, never a traceback (Tasks 9, 11).

---

### Task 1: The `graph.*` config keys

**Depends on:** none

**Files:**
- Modify: `agentflow/config.py` (a new GraphPolicy dataclass beside MemoryPolicy at line 436; a `graph` field on Config; the parse beside `memory=` at line 1743; a `graph` method on the validator, called after its `memory` call at line 3690)
- Modify: `agentflow/defaults/config.yaml` (a `graph:` block after `memory:`)
- Modify: `agentflow/config_edit.py` (a "Knowledge graph" section after "Memory" at line 204)
- Modify: `docs/agentflow-playbook.md` (§14, after the three memory lines at 649 to 651)
- Test: `agentflow/tests/test_config.py`, `agentflow/tests/test_config_edit.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass
  class GraphPolicy:
      enabled: bool = False
      exclude: tuple[str, ...] = ()   # `.gitignore`-style patterns, on top of the fixed list
      max_file_kb: int = 512          # a larger file gets a file node and is not parsed
  # Config.graph: GraphPolicy = field(default_factory=GraphPolicy)
  GRAPH_EXCLUDE_MAX = 64; GRAPH_PATTERN_MAX = 200; GRAPH_FILE_KB = (1, 4096)
  ```

- [ ] **Step 1: Write the failing tests**

```python
# test_config.py
def test_graph_defaults(cfg):
    assert (cfg.graph.enabled, cfg.graph.exclude, cfg.graph.max_file_kb) == (False, (), 512)

@pytest.mark.parametrize("patch, path", [
    ({"enabled": "yes"}, "graph.enabled"),
    ({"max_file_kb": 0}, "graph.max_file_kb"),
    ({"max_file_kb": 4097}, "graph.max_file_kb"),
    ({"max_file_kb": True}, "graph.max_file_kb"),
    ({"exclude": ["/abs"]}, "graph.exclude[0]"),
    ({"exclude": ["a/../b"]}, "graph.exclude[0]"),
    ({"exclude": [""]}, "graph.exclude[0]"),
    ({"exclude": ["x" * 201]}, "graph.exclude[0]"),
    ({"exclude": ["p"] * 65}, "graph.exclude"),
])
def test_graph_keys_are_validated(tmp_cfg, patch, path):
    assert ("error", path) in errors(issues_for(tmp_cfg, {"graph": patch}))   # the file's own helpers

# test_config_edit.py
def test_the_graph_section_saves_into_the_pack(shipped):
    proposal = form(shipped, (["graph", "enabled"], True), (["graph", "exclude"], ["generated/", "*.snap"]))
    assert not proposal.errors, proposal.errors
    config_edit.save(shipped, proposal)
    graph = load_config(shipped).graph
    assert graph.enabled is True and graph.exclude == ("generated/", "*.snap")
# and one row in the "what the form refuses" table: (["graph", "max_file_kb"], 0, EditError)
```

If `load_config` itself refuses a wrong YAML type (`enabled: "yes"`, a string for `exclude`) with a `ConfigError`, assert that for those rows instead (`pytest.raises(ConfigError)` with `exc.value.path == path`), as the file's other type-error tests do.

- [ ] **Step 2: Run them and see them fail**

Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_config.py agentflow/tests/test_config_edit.py --import-mode=prepend -q -k graph`
Expected: FAIL (`Config` has no attribute `graph`).

- [ ] **Step 3: Add `GraphPolicy`, the parse, `Validator.graph`, the defaults block and the Form section**

Defaults block, exact text:

```yaml
graph:
  enabled: false       # index this repository's code and docs for `agentflow graph` (the knowledge graph)
  exclude: []          # `.gitignore`-style patterns left out of the index, on top of `.gitignore` and the fixed list
  max_file_kb: 512     # a larger file is listed and not parsed
```

Form section: `("Knowledge graph", "The index of this repository's code and docs (`agentflow graph`).", [...])` with `graph.enabled` ("Index this repository", `bool`), `graph.exclude` ("Patterns left out (`.gitignore` syntax)", `list`), `graph.max_file_kb` ("Largest file parsed (KB)", `int`, 1 to 4096).

- [ ] **Step 4: Add the three playbook §14 lines**, in the style of the `memory.*` lines: the key, what it does, its default, what `validate` wants, "Not protected."

- [ ] **Step 5: Run the two test files, `bin/agentflow validate` (must print `0 warnings`) and ruff; all pass**

- [ ] **Step 6: Commit** — `Add the graph.enabled, graph.exclude and graph.max_file_kb keys.`

---

### Task 2: `graph_model.py`, the kinds, the facts and their check

**Depends on:** none

**Files:**
- Create: `agentflow/graph_model.py`
- Test: `agentflow/tests/test_graph_model.py`

**Interfaces:**
- Produces (stdlib only; this module is imported by the worker):
  ```python
  SCHEMA_VERSION = 1
  NODE_KINDS = ("file", "symbol", "doc", "section", "decision", "question", "requirement", "package", "provides", "needs")
  EDGE_KINDS = ("contains", "imports", "references", "tests", "depends_on", "mentions")
  LANGUAGES = ("python", "typescript", "tsx", "javascript", "markdown", "manifest")
  EXTRACTOR_VERSIONS = {"python": "1", "typescript": "1", "tsx": "1", "javascript": "1", "markdown": "1", "manifest": "1"}
  NAME_MAX, SIGNATURE_MAX, DOC_MAX, SECTION_TEXT_MAX = 200, 300, 200, 4000
  MAX_DEFS, MAX_IMPORTS, MAX_REFS, MAX_SECTIONS, MAX_LINKS, MAX_DEPS, MAX_NOTES = 5000, 2000, 20000, 2000, 2000, 2000, 20

  @dataclass(frozen=True)
  class Def:     name: str; kind: str; line: int; end: int; signature: str; doc: str; exported: bool   # kind: function|class|method|constant
  class Import:  module: str; name: str; alias: str; level: int; line: int   # one per bound name; `import a.b` binds alias "a"
  class Ref:     name: str; line: int; scope: str      # a dotted name as written; scope is the enclosing Def.name or ""
  class Section: title: str; level: int; line: int; end: int; kind: str; text: str   # kind: section|decision|question|requirement
  class Link:    target: str; line: int                # a relative link target, fragment removed
  class Dep:     name: str; spec: str; group: str      # group: runtime|dev|optional
  class Facts:   lang: str; doc: str; defs: tuple[Def, ...]; imports: tuple[Import, ...]; refs: tuple[Ref, ...]
                 sections: tuple[Section, ...]; links: tuple[Link, ...]; deps: tuple[Dep, ...]
                 package: str; ecosystem: str; status: str; notes: tuple[str, ...]
  def empty(lang: str, *notes: str) -> Facts
  def to_json(facts: Facts) -> dict
  def check_facts(raw: object, lang: str, lines: int) -> Facts | None
  def ident(text: object) -> str | None     # [A-Za-z_$][A-Za-z0-9_$.]{0,199}, else None
  def module(text: object) -> str | None    # [A-Za-z0-9_@./-]{1,200} with no ".." segment, else None
  def text(value: object, limit: int) -> str   # a str with C0/C1 controls and line breaks replaced by one space, cut to limit

  @dataclass(frozen=True)
  class Node: id: str; kind: str; name: str; path: str; line: int; end: int; trust: str; signature: str; text: str
  class Edge: src: str; dst: str; kind: str; confidence: str; origin: str   # origin: the path whose facts made it
  def node_id(repo_id: str, kind: str, path: str, name: str = "") -> str    # "<repo_id>:<kind>:<path>#<name>"
  ```
  `check_facts` returns None when `raw` is not a mapping of exactly the `Facts` fields, `lang` differs, any list is over its cap, any name fails `ident`/`module`, any `kind` is unknown, or any line is not an int with `1 <= line <= end <= lines`. It cleans free text with `text()` rather than refusing it.

- [ ] **Step 1: Write the failing tests** — `to_json` then `check_facts` round-trips a `Facts` holding one of each record; and a table of refusals: a string for `defs`, an extra top-level key, `defs` of 5001 entries, a `Def.name` of `"a b"`, `"a\nb"`, `"é"` and 201 characters, `line` of 0 and of `lines + 1`, `end < line`, a bool where an int belongs, `lang` not the one asked for, `Import.module` of `"../x"`. `text("a\x1b[31mb\nc", 10) == "a [31mb c"`.

- [ ] **Step 2: Run and see them fail** — `pytest agentflow/tests/test_graph_model.py`; FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `graph_model.py`**

- [ ] **Step 4: Run the tests and ruff; all pass**

- [ ] **Step 5: Commit** — `Add graph_model: the knowledge graph's kinds, facts and the check that distrusts them.`

---

### Task 3: `graph_ignore.py` and `graph_select.py`, which tracked paths are indexed

**Depends on:** none

**Files:**
- Create: `agentflow/graph_ignore.py`, `agentflow/graph_select.py`
- Test: `agentflow/tests/test_graph_ignore.py`, `agentflow/tests/test_graph_select.py`

**Interfaces:**
- Produces:
  ```python
  # graph_ignore.py (pure)
  MAX_FILE_BYTES, MAX_LINES, MAX_PATTERN = 256 * 1024, 5000, 512
  class Ignore:
      def __init__(self, files: Mapping[str, str]) -> None   # {directory of the .gitignore ("" = root): its text}
      notes: list[str]                                        # one per file or pattern left out by a cap
      def ignored(self, path: str) -> bool

  # graph_select.py (pure)
  STATES = ("indexed", "unparsed", "ignored", "secret", "vendored", "excluded", "symlink", "submodule")
  def language(path: str) -> str | None
  def is_test(path: str) -> bool
  def state(path: str, mode: str, *, ignore: Ignore, exclude: Ignore) -> str   # one of STATES but "unparsed"; "indexed" = may have nodes
  def parses(path: str, doc_roots: Sequence[str]) -> str | None                # the language to parse it as, or None for a file node only
  ```
- `state` checks in this order and stops at the first hit: mode `120000` → `symlink`; mode `160000` → `submodule`; a secret pattern on the basename, case-insensitively (`.env`, `.env.*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.keystore`, `id_rsa*`, `id_ed25519*`, `*credentials*`) → `secret`; `ignore.ignored(path)` → `ignored`; a path segment in the fixed list (`node_modules`, `vendor`, `third_party`, `bower_components`, `site-packages`, `.venv`, `venv`, `__pycache__`) or a basename matching `*.min.js`, `*.min.css`, `*.map`, `*_pb2.py`, `*_pb2_grpc.py`, `*.lock`, `package-lock.json`, `pnpm-lock.yaml` → `vendored`; `exclude.ignored(path)` → `excluded`; else `indexed`.
- `language`: `.py` → `python`; `.ts`, `.mts`, `.cts` → `typescript`; `.tsx` → `tsx`; `.js`, `.mjs`, `.cjs`, `.jsx` → `javascript`; `.md` → `markdown`; basename `pyproject.toml`, `package.json` or `requirements*.txt` → `manifest`; else None.
- `parses` returns `language(path)`, except `markdown` only when the path has no `/` or lies under one of `doc_roots`.
- `is_test`: basename `test_*.py`, `*_test.py`, `*.test.<ext>` or `*.spec.<ext>` for the JavaScript and TypeScript extensions, or a path segment `tests` or `__tests__`.

**The matching rules `Ignore` must follow** (git's, stated here because they are not derivable from the signature):
1. A line is skipped when blank or starting with `#`. Trailing spaces are dropped unless escaped with `\`. A leading `!` negates; a leading `\` escapes a `#` or `!`.
2. A trailing `/` makes the pattern match directories only. A pattern with no other `/` matches at any depth below its file's directory; a pattern with a leading or inner `/` is anchored to that directory.
3. `*` matches within one segment, `?` one character but `/`, `[...]` a class; a leading `**/` matches any directories, a trailing `/**` everything below, `/**/` zero or more directories.
4. For one path: walk its ancestor directories from the top, then the path itself. For each, the verdict is the **last** matching pattern among all applicable `.gitignore` files, read from the shallowest file to the deepest. If any ancestor directory ends up ignored, the path is ignored and nothing below can re-include it.
5. A file over `MAX_FILE_BYTES` or `MAX_LINES` contributes no pattern and one note. A pattern over `MAX_PATTERN` is skipped with a note. Each pattern becomes one regular expression in which `*` is `[^/]*` and `**` segments are `(?:.*/)?` or `.*`, so no repetition is nested.

- [ ] **Step 1: Write the failing tests**

`test_graph_ignore.py`: one parametrized table checked two ways. (a) `Ignore(files).ignored(path)` equals the expected bool. (b) In a temp git repository holding the same `.gitignore` files, `git check-ignore --no-index -q <path>` gives the same answer (exit 0 = ignored). The table, each row `(files, path, expected)`:

```python
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
]
```

Plus: a `.gitignore` of 300 KB gives one note and ignores nothing; a 600-character pattern is skipped with a note and the other patterns still apply; the pattern `a*a*a*a*a*a*a*a*b` against a path of 5,000 `a` characters returns within one second.

`test_graph_select.py`: the `state` order (a symlink named `.env` is `symlink`; `.ENV.local` is `secret`; `config/credentials.json` is `secret` even when not ignored; an ignored path is `ignored` before `vendored`; `x/node_modules/a.js` is `vendored`; a `graph.exclude` hit is `excluded`); `language` and `is_test` for each extension and pattern above; `parses("docs/a.md", ["docs"]) == "markdown"`, `parses("pkg/notes.md", ["docs"]) is None`, `parses("README.md", []) == "markdown"`.

- [ ] **Step 2: Run and see them fail**

- [ ] **Step 3: Implement both modules**

- [ ] **Step 4: Run the two test files, `test_portability.py` and ruff; all pass**

- [ ] **Step 5: Commit** — `Decide which tracked paths the knowledge graph indexes, honouring each repository's .gitignore.`

---

### Task 4: `graph_python.py`, Python facts from `ast`

**Depends on:** Task 2

**Files:**
- Create: `agentflow/graph_python.py`
- Test: `agentflow/tests/test_graph_python.py`

**Interfaces:**
- Consumes: `graph_model.Facts`, `Def`, `Import`, `Ref`, `empty`, `ident`, `text`.
- Produces: `def extract(path: str, data: bytes) -> Facts` (pure, never raises).

Rules the tests pin: definitions are module-level functions, async functions and classes, methods one level deep as `Class.method`, and module-level `UPPER_CASE = ...` assignments as `constant`; nothing nested deeper. `signature` is the `def` or `class` line rebuilt from the tree (`ast.unparse` of the arguments and return annotation), with no body. `doc` is the first line of the docstring. `exported` is "the name does not start with `_`". `Facts.doc` is the first line of the module docstring. One `Import` per bound name. A `Ref` is a dotted name in load context whose first part is an import alias or a module-level definition of this file; one per `(name, scope)`, first line kept. A `SyntaxError`, `ValueError`, `RecursionError` or `MemoryError` gives `empty("python", "<the error's class name>")`.

- [ ] **Step 1: Write the failing tests**

```python
SRC = b'''"""Run cap: hold a run until a slot frees.

More text."""
import os.path
import a.b as ab
from . import sibling
from ..pkg.mod import helper as h, Other
LIMIT = 64
_private = 1

class Cap(Base):
    """Holds one slot."""
    def hold(self, run: "Run", *, wait: float = 1.0) -> bool:
        return h(run) and ab.go(os.path.join("a"))
    def _free(self): ...

async def main(argv: list[str] | None = None) -> int:
    def inner(): return Other()
    return Cap().hold(None)
'''

def test_definitions():
    f = extract("pkg/run_cap.py", SRC)
    assert f.doc == "Run cap: hold a run until a slot frees."
    assert [(d.name, d.kind, d.exported) for d in f.defs] == [
        ("LIMIT", "constant", True), ("Cap", "class", True), ("Cap.hold", "method", True),
        ("Cap._free", "method", False), ("main", "function", True)]
    hold = f.defs[2]
    assert hold.signature == "def hold(self, run: 'Run', *, wait: float=1.0) -> bool"
    assert (hold.line, hold.end) == (13, 14) and f.defs[1].doc == "Holds one slot."

def test_imports():
    assert [(i.module, i.name, i.alias, i.level) for i in extract("pkg/run_cap.py", SRC).imports] == [
        ("os.path", "", "os", 0), ("a.b", "", "ab", 0), ("", "sibling", "sibling", 1),
        ("pkg.mod", "helper", "h", 2), ("pkg.mod", "Other", "Other", 2)]

def test_references():
    refs = {(r.name, r.scope) for r in extract("pkg/run_cap.py", SRC).refs}
    assert refs == {("h", "Cap.hold"), ("ab.go", "Cap.hold"), ("os.path.join", "Cap.hold"),
                    ("Other", "main"), ("Cap", "main")}     # `Base` is neither imported nor defined here: no Ref
```

A name used inside a nested function belongs to the enclosing top-level definition's scope (`Other` above). Plus: `b"def f(:\n"` gives `empty("python", "SyntaxError")`; a file of 3,000 nested parentheses gives a note and no exception; bytes that are not UTF-8 give a note; a definition named `café` is dropped and one note counts it; 6,000 functions are cut to `MAX_DEFS` with a note.

- [ ] **Step 2: Run and see them fail**

- [ ] **Step 3: Implement `extract`** with `ast.parse(data, filename="<blob>")` (never the real path, never `compile` or `exec`).

- [ ] **Step 4: Run the tests and ruff; all pass**

- [ ] **Step 5: Commit** — `Extract Python definitions, imports and references with ast.`

---

### Task 5: `graph_markdown.py` and `graph_manifest.py`

**Depends on:** Task 2

**Files:**
- Create: `agentflow/graph_markdown.py`, `agentflow/graph_manifest.py`
- Test: `agentflow/tests/test_graph_markdown.py`, `agentflow/tests/test_graph_manifest.py`

**Interfaces:**
- Produces: `graph_markdown.extract(path: str, data: bytes) -> Facts` and `graph_manifest.extract(path: str, data: bytes) -> Facts` (pure, never raise).

Markdown rules: a `Section` per ATX heading outside a code fence, running to the next heading of the same or a higher level, `text` being its own body up to its first sub-heading, cut to `SECTION_TEXT_MAX`. Under a heading whose title starts with `Decisions`, or under a line `**Resolved (...)**`, each list item is a `decision`; under `Open Questions`, each list item before the first `**Resolved` line is a `question`; under `Success criteria`, each list item is a `requirement`. `Facts.status` is the value of the first `Status:` line before the first `## ` heading. `Facts.doc` is the first `# ` title. A `Link` is each `[text](target)` whose target is relative (no scheme, no leading `/` or `#`), with its fragment and query removed.

Manifest rules: `pyproject.toml` (`tomllib`): `package` from `[project] name`, `ecosystem` `pypi`, a `Dep` per `[project] dependencies` entry (`runtime`) and per `[project.optional-dependencies]` entry (`optional`), `name` being the requirement's name lower-cased with `_` and `.` as `-`, `spec` the rest. `requirements*.txt`: a `Dep` per line that is not blank, a comment or an option (`-r`, `-e`, `--…`), `ecosystem` `pypi`, no `package`. `package.json` (`json`): `package` from `name`, `ecosystem` `npm`, `dependencies` (`runtime`), `devDependencies` (`dev`), `optionalDependencies` and `peerDependencies` (`optional`). Anything malformed gives `empty("manifest", "<reason>")`.

- [ ] **Step 1: Write the failing tests**

```python
DOC = b"""# Proposal: a thing

Status: approved (2026-11-28)

## Design
Body text. See [the plan](../plans/p.md#task-1) and [site](https://example.com).

### Part
Inner.

```
## not a heading
```

## Decisions (grilling, 2026-11-28)
1. **One index.** Because.
2. **No embeddings.**

## Open Questions
- Is it fast?

**Resolved (2026-11-28, grilling)**
- **Q1 - Size.** Small.

## Success criteria
1. It builds.
"""

def test_markdown():
    f = graph_markdown.extract("docs/x.md", DOC)
    assert (f.doc, f.status) == ("Proposal: a thing", "approved (2026-11-28)")
    kinds = [(s.kind, s.title) for s in f.sections]
    assert ("section", "Design") in kinds and ("section", "Part") in kinds and ("section", "not a heading") not in kinds
    assert [s.title for s in f.sections if s.kind == "decision"] == ["One index. Because.", "No embeddings.", "Q1 - Size. Small."]
    assert [s.title for s in f.sections if s.kind == "question"] == ["Is it fast?"]
    assert [s.title for s in f.sections if s.kind == "requirement"] == ["It builds."]
    assert [l.target for l in f.links] == ["../plans/p.md"]
    design = next(s for s in f.sections if s.title == "Design")
    assert design.text.startswith("Body text.") and "Inner." not in design.text

def test_manifests():
    py = graph_manifest.extract("pyproject.toml", b'[project]\nname = "My_Pkg"\ndependencies = ["PyYAML>=6", "requests[socks] ; python_version<\\"4\\""]\n[project.optional-dependencies]\ndev = ["pytest"]\n')
    assert (py.package, py.ecosystem) == ("my-pkg", "pypi")
    assert [(d.name, d.group) for d in py.deps] == [("pyyaml", "runtime"), ("requests", "runtime"), ("pytest", "optional")]
    js = graph_manifest.extract("web/package.json", b'{"name": "@acme/web", "dependencies": {"react": "^19"}, "devDependencies": {"vitest": "1"}}')
    assert (js.package, js.ecosystem) == ("@acme/web", "npm")
    assert [(d.name, d.spec, d.group) for d in js.deps] == [("react", "^19", "runtime"), ("vitest", "1", "dev")]
    req = graph_manifest.extract("requirements-dev.txt", b"# c\n-r base.txt\nruff==0.17.4\n\n")
    assert [(d.name, d.spec) for d in req.deps] == [("ruff", "==0.17.4")]
```

Plus: a 2 MB section is cut to `SECTION_TEXT_MAX`; 3,000 headings are cut to `MAX_SECTIONS` with a note; `package.json` that is a JSON array, `pyproject.toml` with a syntax error, and a `dependencies` value that is a string each give an `empty` with a note; a dependency name with a space is dropped.

- [ ] **Step 2: Run and see them fail**

- [ ] **Step 3: Implement both modules**

- [ ] **Step 4: Run the tests and ruff; all pass**

- [ ] **Step 5: Commit** — `Extract doc sections, decisions and links from Markdown, and dependencies from Python and Node manifests.`

---

### Task 6: `graph_worker.py` and `graph_parse.py`, the parser process and its keeper

**Depends on:** Task 4, Task 5

**Files:**
- Create: `agentflow/graph_worker.py`, `agentflow/graph_parse.py`
- Test: `agentflow/tests/test_graph_parse.py`, `agentflow/tests/fixtures/graph/bad_worker.py`

**Interfaces:**
- Consumes: the three `extract` functions, `graph_model.check_facts`, `to_json`, `empty`.
- Produces:
  ```python
  # graph_parse.py
  PARSE_TIMEOUT_S, MAX_RESTARTS, RSS_MAX_MB, REPLY_MAX_BYTES = 10, 20, 1024, 4 * 1024 * 1024
  WORKER = Path(__file__).with_name("graph_worker.py")
  def worker_argv(mode: str) -> list[str] | None   # mode: "locked" | "offline" | "stdlib"; None when `uv` is not on PATH for a uv mode
  def worker_env() -> dict[str, str]               # PATH, HOME, LANG=C.UTF-8, and UV_CACHE_DIR / XDG_CACHE_HOME when set; nothing else
  class Parser:
      def __init__(self, *, download: bool, argv: list[str] | None = None) -> None
      mode: str                    # the mode that answered the handshake
      languages: dict[str, str]    # {"python": "1", "typescript": "1+0.23.2", ...}: the extractor version per language it can parse
      def parse(self, path: str, lang: str, data: bytes) -> Facts   # never raises; a failure is `empty(lang, <note>)`
      def close(self) -> None
  ```
- **The protocol.** The worker writes one handshake line first: `{"ready": 1, "pid": <its own pid>, "languages": {...}}`. Each request is one JSON line `{"id": n, "path": str, "lang": str, "size": n}` followed by exactly `size` bytes; each reply is one JSON line `{"id": n, "facts": {...}}` of at most `REPLY_MAX_BYTES`.
- **Starting it.** `"stdlib"` is `[sys.executable, "-I", "-S", WORKER]`. `"locked"` is `[uv, "run", "--quiet", "--locked", "--python", sys.executable, "--script", WORKER]`, and `"offline"` adds `--offline`. `Parser` tries `"locked"` when `download` else `"offline"`, and falls back to `"stdlib"` when the handshake does not arrive within 120 s (`locked`) or 20 s (`offline`). The child gets `cwd` = a fresh empty temp directory, `env=worker_env()`, `stderr` to `/dev/null`, and its own session (`start_new_session=True`) so the whole group can be killed. `argv` overrides the command, for tests.
- **Keeping it.** While waiting for a reply the parent wakes every 0.5 s. Past `PARSE_TIMEOUT_S` it kills the group and returns `empty(lang, "parser_timeout")`. When the resident size of the handshake's `pid` (`ps -o rss= -p <pid>`) is over `RSS_MAX_MB` it kills and returns `empty(lang, "parser_memory")`. End of file, a line over `REPLY_MAX_BYTES`, text that is not JSON, a different `id`, or facts `check_facts` refuses give `empty(lang, "parser_crashed")` or `"parser_reply"`; the worker is restarted for the next file. After `MAX_RESTARTS` restarts every later `parse` returns `empty(lang, "parser_unavailable")` at once. A `lang` missing from `languages` returns `empty(lang, "no_grammar")` without a request.
- **The worker.** It removes its own directory from `sys.path` and inserts the directory above it, so it imports `agentflow.graph_model` and the extractors and nothing can shadow the stdlib. It sets `RLIMIT_AS` to 1 GiB inside `try/except (ValueError, OSError)` (macOS refuses it). It reads from `sys.stdin.buffer`, dispatches on `lang`, and writes `to_json(facts)`. It opens no file and no socket. In K1 it offers `python`, `markdown` and `manifest`; Task 7 adds the tree-sitter languages.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.fixture
def parser():
    p = Parser(download=False, argv=worker_argv("stdlib"))
    yield p
    p.close()

def test_stdlib_worker_parses_python_markdown_and_manifests(parser):
    assert parser.mode == "stdlib" and set(parser.languages) == {"python", "markdown", "manifest"}
    assert [d.name for d in parser.parse("a.py", "python", b"def f(): pass\n").defs] == ["f"]
    assert parser.parse("a.md", "markdown", b"# T\n").doc == "T"
    assert parser.parse("package.json", "manifest", b'{"name": "x"}').package == "x"

def test_a_language_without_a_grammar_is_a_note(parser):
    assert parser.parse("a.ts", "typescript", b"export const a = 1").notes == ("no_grammar",)

def test_the_stdlib_worker_imports_no_third_party_package(tmp_path):
    # `-I -S` gives it no site-packages: a third-party import would stop the handshake
    out = subprocess.run(worker_argv("stdlib"), input=b"", capture_output=True, cwd=tmp_path, env=worker_env(), timeout=30)
    assert json.loads(out.stdout.splitlines()[0])["ready"] == 1

def test_worker_env_carries_nothing_else(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/evil"); monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    assert set(worker_env()) <= {"PATH", "HOME", "LANG", "UV_CACHE_DIR", "XDG_CACHE_HOME"}
```

`fixtures/graph/bad_worker.py` is a scripted stand-in chosen by its first argument, used through `Parser(download=False, argv=[sys.executable, BAD, "<behaviour>"])`: `hang` (handshake, then sleep), `half` (half a reply, then sleep), `huge` (one 5 MB line), `wrong_id`, `other_path` (valid facts whose `lang` is another language), `not_json`, `exit` (handshake, then exit), `no_handshake`. Tests, with `monkeypatch.setattr(graph_parse, "PARSE_TIMEOUT_S", 1)`:

- `hang` and `half` → `("parser_timeout",)`, within 3 s, and the child's process group is gone.
- `huge`, `wrong_id`, `other_path`, `not_json` → `("parser_reply",)`; `exit` → `("parser_crashed",)`.
- after any of them the next `parse` gets a fresh worker and, for `exit`, the same note again; after `MAX_RESTARTS` (set to 2 in the test) the note is `parser_unavailable` and no process is started.
- `no_handshake` given as `argv` leaves `languages == {}`, and every parse returns `no_grammar` without waiting (an override has no fallback). With no override and `uv` taken off `PATH`, `Parser(download=False)` ends in `stdlib` mode.
- real worker: `parser.parse("deep.py", "python", b"(" * 200000)` returns a note and the next parse still works.
- memory: with `RSS_MAX_MB` set to 1, a real parse of a 200 KB Python file returns `parser_memory` on a system where `ps` reports the size (skip with the reason where it does not).
- `close()` leaves no child process and removes the temp directory.

- [ ] **Step 2: Run and see them fail**

- [ ] **Step 3: Implement `graph_worker.py` and `graph_parse.py`**

A reader thread puts stdout lines on a `queue.Queue` (each read bounded by `REPLY_MAX_BYTES + 1`); the main thread waits on it with a 0.5 s timeout so it can check the clock and the resident size. Kill with `os.killpg(proc.pid, signal.SIGKILL)`, then `wait`.

- [ ] **Step 4: Run the tests, `test_portability.py` and ruff; all pass**

- [ ] **Step 5: Commit** — `Parse repository bytes in a limited child process and distrust everything it returns.`

---

### Task 7: `graph_syntax.py` and the locked grammars, TypeScript and JavaScript facts

**Depends on:** Task 6

**Files:**
- Create: `agentflow/graph_syntax.py`, `agentflow/graph_worker.py.lock` (generated)
- Modify: `agentflow/graph_worker.py` (the script header; offer the tree-sitter languages)
- Test: `agentflow/tests/test_graph_syntax.py`

**Interfaces:**
- Produces:
  ```python
  # graph_syntax.py (imported only by the worker; imports tree_sitter lazily)
  PINS = {"tree-sitter": "0.27.0", "tree-sitter-javascript": "0.25.0", "tree-sitter-typescript": "0.23.2"}
  def available() -> dict[str, str]                      # {"javascript": "1+0.25.0", "typescript": "1+0.23.2", "tsx": "1+0.23.2"}, or {} when tree_sitter is missing
  def extract(path: str, data: bytes, lang: str) -> Facts  # pure, never raises
  ```
- The worker's header, exact text:
  ```python
  # /// script
  # requires-python = ">=3.12"
  # dependencies = ["tree-sitter==0.27.0", "tree-sitter-javascript==0.25.0", "tree-sitter-typescript==0.23.2"]
  # ///
  ```
- The API, checked on 2026-11-28: `Parser(Language(tree_sitter_javascript.language()))`, `tree_sitter_typescript.language_typescript()` and `.language_tsx()`; `parser.parse(data).root_node`, then walk `node.children` by `node.type` (`import_statement`, `export_statement`, `function_declaration`, `class_declaration`, `method_definition`, `lexical_declaration`, `interface_declaration`, `type_alias_declaration`, `enum_declaration`, `call_expression`). Positions are `node.start_point[0] + 1`.

Rules the tests pin: top-level `function`, `class`, `interface`, `type`, `enum` and `const|let` bound to an arrow function or function expression are definitions (`function`, `class`, or `constant` for other top-level `const NAME`), and class methods are `Class.method`; `exported` is "declared under `export`" (`export default` included, named `default` when anonymous). `signature` is the declaration's first line up to its body, cut to `SIGNATURE_MAX`. `doc` is the first line of a `/** … */` comment directly above. Imports: one `Import` per binding of `import x from`, `import {a as b} from`, `import * as ns from`, and `const x = require("m")`, with `module` the specifier as written and `level` 0; `export … from "m"` is an import too. A `Ref` is a call or member chain whose first part is an import binding or a top-level definition. A tree with `root_node.has_error` still yields what was found, with the note `syntax_error`.

- [ ] **Step 1: Write the failing tests** (through the worker, so they exercise the real launch)

```python
@pytest.fixture(scope="module")
def exact():
    p = Parser(download=bool(os.environ.get("CI")))     # CI may download; a developer machine uses its cache
    if "typescript" not in p.languages:
        p.close()
        if os.environ.get("CI"):
            pytest.fail("the locked grammars did not start in CI")
        pytest.skip("the tree-sitter grammars are not in uv's cache: run `agentflow graph build` once with network access")
    yield p
    p.close()

TS = b'''import React, { useState as useS } from "react";
import * as api from "./api/client";
import type { User } from "../types";
export { helper } from "./helper.js";
const limit = 5;
/** Loads one user. */
export async function loadUser(id: string): Promise<User> { return api.get(id); }
export const format = (u: User): string => String(u);
export default class Store<T> implements Base { private items: T[] = []; add(item: T): void { useS(item); } }
export interface Base { add(item: unknown): void }
'''

def test_typescript(exact):
    f = exact.parse("web/store.ts", "typescript", TS)
    assert [(d.name, d.kind, d.exported) for d in f.defs] == [
        ("limit", "constant", False), ("loadUser", "function", True), ("format", "function", True),
        ("Store", "class", True), ("Store.add", "method", True), ("Base", "class", True)]
    assert f.defs[1].doc == "Loads one user." and f.defs[1].signature == "export async function loadUser(id: string): Promise<User>"
    assert [(i.module, i.name, i.alias) for i in f.imports] == [
        ("react", "default", "React"), ("react", "useState", "useS"), ("./api/client", "*", "api"),
        ("../types", "User", "User"), ("./helper.js", "helper", "helper")]
    assert {(r.name, r.scope) for r in f.refs} >= {("api.get", "loadUser"), ("useS", "Store.add")}
```

Plus: a `.tsx` file with a component returning JSX parses with no `syntax_error`; a JavaScript file with `const x = require("./x")` and `module.exports = { run }` yields the import and `run` as exported; `export const a = 1,` (broken) yields the note `syntax_error` and still the imports above it; `parser.languages["typescript"] == "1+0.23.2"`; `parser.mode in ("offline", "locked")`.

- [ ] **Step 2: Run and see them fail (or skip without the cache)**

- [ ] **Step 3: Add the header, implement `graph_syntax.py`, and have the worker merge `graph_syntax.available()` into its handshake**

- [ ] **Step 4: Generate the lock**

Run: `uv lock --script agentflow/graph_worker.py`
Expected: `agentflow/graph_worker.py.lock` exists and `grep -c 'hash = ' agentflow/graph_worker.py.lock` prints a number above 20. Read the lock and confirm it lists only `tree-sitter`, `tree-sitter-javascript` and `tree-sitter-typescript`, each from `files.pythonhosted.org`.

- [ ] **Step 5: Check the lock is enforced**

Run: `cp agentflow/graph_worker.py.lock /tmp/k1.lock && sed -i.bak 's/tree-sitter==0.27.0/tree-sitter==0.26.0/' agentflow/graph_worker.py && uv run --locked --script agentflow/graph_worker.py </dev/null; echo "exit=$?"`
Expected: ``error: The lockfile at `uv.lock` needs to be updated, but `--locked` was provided.`` and `exit=1` (checked on `uv` 0.13.8), and the lock file unchanged (`cmp /tmp/k1.lock agentflow/graph_worker.py.lock`). Then restore: `mv agentflow/graph_worker.py.bak agentflow/graph_worker.py`. Record the command's output in the pull request.

- [ ] **Step 6: Run `test_graph_syntax.py`, `test_graph_parse.py`, `test_packaging.py`, `test_portability.py` and ruff; all pass**

`test_packaging.py` must pass unchanged: `pyproject.toml` still declares only `pyyaml`, and the wheel holds the worker and its lock.

- [ ] **Step 7: Commit** — `Parse TypeScript and JavaScript with tree-sitter in the locked parser worker.`

---

### Task 8: `graph_resolve.py`, facts into nodes and edges

**Depends on:** Task 2, Task 3

**Files:**
- Create: `agentflow/graph_resolve.py`
- Test: `agentflow/tests/test_graph_resolve.py`

**Interfaces:**
- Consumes: `graph_model` (`Facts`, `Node`, `Edge`, `node_id`), `graph_select.is_test`.
- Produces (pure):
  ```python
  def provided(path: str, facts: Facts) -> frozenset[str]   # keys other files may resolve to
  def wanted(path: str, facts: Facts) -> frozenset[str]     # every key this file's imports and links could resolve to
  class Index(Protocol):
      def providers(self, key: str) -> Sequence[str]        # paths that provide `key`, sorted
      def facts(self, path: str) -> Facts | None
  def resolve(repo_id: str, path: str, facts: Facts, index: Index) -> tuple[list[Node], list[Edge]]
  def file_node(repo_id: str, path: str, lang: str | None) -> Node   # for a path that is listed and not parsed
  ```
- **Keys.** Every file provides `path:<its path>`. A Python file `a/b/c.py` also provides `py:c`, `py:b.c` and `py:a.b.c`; `a/b/__init__.py` provides `py:b` and `py:a.b`. A manifest that declares a package provides `pkg:<ecosystem>/<name>`.
- **Wants.** A Python `Import` with `level` 0 wants `py:<module>` and, when it has a `name`, `py:<module>.<name>`. With `level` n it wants `path:<n-1 directories up from the file's directory>/<module as a path>.py`, the same with `/__init__.py`, and the two forms with `/<name>` appended. A JavaScript or TypeScript import whose specifier starts with `.` wants `path:<the normalised target>` with each of `""`, `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs`, `/index.ts`, `/index.tsx`, `/index.js` appended, and, for a specifier ending in `.js`, the same stem with `.ts` and `.tsx`. A bare specifier wants nothing. A Markdown `Link` wants `path:<the target normalised against the file's directory>`. A target that leaves the repository (`..` past the root) wants nothing.
- **Choosing a provider.** One provider: take it. Several (two packages with a module of the same name): take the one sharing the longest leading directory with the importing file; a tie is no edge.
- **Nodes.** One `file` node per parsed file (`name` its basename, `text` = `facts.doc`); a `symbol` per `Def`; for Markdown one `doc` node and one node per `Section` of its kind (`trust` `human`, everything else `code`); for a manifest a `package` node when it names one, a `provides` node (`pkg:` key as its name) and a `needs` node per `Dep`.
- **Edges**, all with `origin` = `path`: `contains` file → symbol and doc → section (`exact`); `imports` file → the chosen file (`exact`); `mentions` doc → the linked file or doc (`exact`); `depends_on` package (or the file when unnamed) → each `needs` (`exact`); `references` from the `Ref`'s scope symbol (or the file) to the definition it names in the chosen file or in this file (`heuristic`); `tests`, when `is_test(path)`, to each imported file that is not itself a test (`heuristic`).

- [ ] **Step 1: Write the failing tests** — a small in-memory `Index` over a dict of `{path: Facts}` built with the real extractors:

```python
FILES = {
    "pkg/__init__.py": b"",
    "pkg/cap.py": b'"""Run cap."""\nfrom .slots import lease\nfrom pkg import util\ndef hold():\n    return lease() and util.now()\n',
    "pkg/slots.py": b"def lease(): ...\n",
    "pkg/util.py": b"def now(): ...\n",
    "tests/test_cap.py": b"from pkg.cap import hold\ndef test_hold(): assert hold()\n",
    "other/util.py": b"def now(): ...\n",
    "docs/a.md": b"# A\nSee [cap](../pkg/cap.py) and [gone](missing.md).\n",
}

def test_keys():
    assert provided("pkg/cap.py", F["pkg/cap.py"]) == {"path:pkg/cap.py", "py:cap", "py:pkg.cap"}
    assert {"path:pkg/slots.py", "path:pkg/slots/__init__.py", "py:pkg.util", "py:pkg"} <= wanted("pkg/cap.py", F["pkg/cap.py"])

def test_edges():
    nodes, edges = resolve("r", "pkg/cap.py", F["pkg/cap.py"], IDX)
    e = {(x.kind, x.src.split(":", 2)[2], x.dst.split(":", 2)[2], x.confidence) for x in edges}
    assert ("imports", "pkg/cap.py#", "pkg/slots.py#", "exact") in e
    assert ("imports", "pkg/cap.py#", "pkg/util.py#", "exact") in e          # pkg/util.py, not other/util.py: nearer directory
    assert ("references", "pkg/cap.py#hold", "pkg/slots.py#lease", "heuristic") in e
    assert ("references", "pkg/cap.py#hold", "pkg/util.py#now", "heuristic") in e
    assert next(n for n in nodes if n.kind == "file").text == "Run cap."

def test_tests_and_mentions():
    _, edges = resolve("r", "tests/test_cap.py", F["tests/test_cap.py"], IDX)
    assert ("tests", "heuristic") in {(x.kind, x.confidence) for x in edges if x.dst.endswith("pkg/cap.py#")}
    _, edges = resolve("r", "docs/a.md", F["docs/a.md"], IDX)
    assert [x.dst.split(":", 2)[2] for x in edges if x.kind == "mentions"] == ["pkg/cap.py#"]
```

Plus: two providers at equal distance give no edge; a JavaScript import of `./api/client` resolves to `api/client.ts` and of `./helper.js` to `helper.ts`; a link to `../../outside.md` from `docs/a.md` wants nothing; every edge of `resolve(path)` has `origin == path`; `resolve` is deterministic (same input, same lists in the same order).

- [ ] **Step 2: Run and see them fail**

- [ ] **Step 3: Implement `graph_resolve.py`**

- [ ] **Step 4: Run the tests and ruff; all pass**

- [ ] **Step 5: Commit** — `Resolve files' facts into the knowledge graph's nodes and edges.`

---

### Task 9: `graph_store.py`, the snapshot and the search

**Depends on:** Task 2

**Files:**
- Create: `agentflow/graph_store.py`
- Test: `agentflow/tests/test_graph_store.py`

**Interfaces:**
- Consumes: `graph_model`, `safeio.ensure_dir`.
- Produces:
  ```python
  FACTS_KEEP = 2000      # cached facts kept for blobs no longer in the tree (a revert is then not parsed again)
  QUERY_TOKENS = 16
  class Unusable(Exception): ...          # missing, not SQLite, another schema version, or no FTS5
  def fts5_available() -> bool
  def path_for(state_dir: Path) -> Path   # <state dir>/graph/graph.sqlite
  def open_read(path: Path) -> sqlite3.Connection          # read-only (`mode=ro`); raises Unusable
  def begin(path: Path, fresh: bool = False) -> tuple[sqlite3.Connection, Path]   # a writable copy beside `path`; a new schema when `fresh` or `path` is Unusable; removes stale temp files
  def publish(conn: sqlite3.Connection, temp: Path, path: Path) -> None   # commit, close, fsync, os.replace
  def discard(conn: sqlite3.Connection, temp: Path) -> None
  @dataclass(frozen=True)
  class FileRow: path: str; blob: str; mode: str; lang: str; state: str; version: str; note: str
  class Hit: node: Node; score: float
  def meta(conn) -> dict[str, str];  def set_meta(conn, **values: str) -> None
  def files(conn) -> dict[str, FileRow]
  def put_file(conn, row: FileRow) -> None
  def drop_file(conn, path: str) -> None     # the row, its nodes, the edges it originated, its keys and its search rows
  def facts(conn, blob: str, lang: str, version: str) -> Facts | None;  def put_facts(conn, blob, lang, version, facts) -> None
  def prune_facts(conn) -> int
  def set_keys(conn, path: str, provided: Iterable[str], wanted: Iterable[str]) -> None
  def provided_by(conn, path: str) -> set[str];  def wanting(conn, keys: Iterable[str]) -> set[str];  def providers(conn, key: str) -> list[str]
  def replace_graph(conn, path: str, nodes: Sequence[Node], edges: Sequence[Edge]) -> None
  def ignore_files(conn) -> dict[str, str];  def set_ignore_file(conn, directory: str, text: str | None) -> None
  def search(conn, query: str, kind: str | None = None, limit: int = 10) -> list[Hit]
  def node(conn, node_id: str) -> Node | None;  def nodes_at(conn, path: str) -> list[Node]
  def neighbours(conn, node_id: str, depth: int = 1) -> list[tuple[Edge, Node]]   # depth 1 to 3, at most 200 rows
  def counts(conn) -> dict[str, int]         # files by state, nodes, edges
  def dump(conn) -> dict[str, list[tuple]]   # every table's rows, sorted: what the delta-equals-full test compares
  ```
- Tables: `meta(key, value)`, `files`, `facts(blob, lang, version, json, seen)`, `provides(key, path)`, `wants(key, path)`, `nodes` (with the `blob` of its file's row), `edges`, `ignore_files(directory, text)`, and `search`, an FTS5 table `(node_id UNINDEXED, name, words, path, text)` with `tokenize='unicode61'`. `words` holds the name split at camel-case boundaries, lower-cased.
- `meta` keys written by `begin` on a fresh schema: `schema` = `SCHEMA_VERSION`. `open_read` raises `Unusable` when `schema` differs.
- **Search.** The query is lower-cased and reduced to tokens of `[a-z0-9]+` (camel-case words split first), at most `QUERY_TOKENS`; no token means `[]`. The `MATCH` argument is the tokens, each in double quotes, joined with ` OR `, passed as one bound parameter. Rank is `bm25(search, 0.0, 8.0, 6.0, 3.0, 1.0)`. `kind` filters by `nodes.kind`, bound.
- The graph directory is created `0700` and the file `0600`.

- [ ] **Step 1: Write the failing tests**

- `begin` on a missing path gives an empty schema; `publish` makes `open_read` work; the published file's mode is `0600`.
- A reader opened before a second `begin` … `publish` still reads the first snapshot's rows, and a reader opened after reads the new ones.
- `discard` leaves the published file byte-identical; a temp file left by a killed build is removed by the next `begin`.
- `open_read` raises `Unusable` for: a missing file, a directory at that path, 100 random bytes, a database whose `schema` is `"999"`. A connection from `open_read` refuses `INSERT` (`sqlite3.OperationalError`).
- `replace_graph` then `drop_file` leaves no node, edge, key or search row for that path, and leaves another path's rows alone; calling `replace_graph` twice for a path leaves one copy.
- `wanting`, `providers`, `provided_by` over three files; `facts` round-trips and misses on another `version`; `prune_facts` keeps every blob that a `files` row names and at most `FACTS_KEEP` others, dropping the least recently `seen`.
- Search: nodes named `run_cap.py`, `RunCapHold` and `governor.py` (text "holds runs on usage windows"); `search(conn, "run cap")` returns the first two before the third; `search(conn, "usage window")` returns `governor.py`; `kind="symbol"` filters; `limit` holds.
- Hostile queries return `[]` or hits and never raise: `""`, `'"'`, `"* OR NEAR("`, `"a" * 5000`, `"'; DROP TABLE nodes; --"`, `"\x00"`; after each, `counts` is unchanged.
- `neighbours(depth=5)` behaves as depth 3 and returns at most 200 rows.

- [ ] **Step 2: Run and see them fail**

- [ ] **Step 3: Implement `graph_store.py`**

- [ ] **Step 4: Run the tests and ruff; all pass**

- [ ] **Step 5: Commit** — `Keep the knowledge graph as one SQLite snapshot per repository, searched with FTS5.`

---

### Task 10: `graph_git.py` and `graph_build.py`, the delta build

**Depends on:** Task 3, Task 6, Task 7, Task 8, Task 9

**Files:**
- Create: `agentflow/graph_git.py`, `agentflow/graph_build.py`
- Test: `agentflow/tests/test_graph_git.py`, `agentflow/tests/test_graph_build.py`

**Interfaces:**
- Consumes: everything above; `guards.HARDENED`; `pack.state_dir`, `pack.repo_id`; `config.Config` (`graph`, `project.base_branch`, `project.docs`).
- Produces:
  ```python
  # graph_git.py
  @dataclass(frozen=True)
  class Entry:  path: str; mode: str; blob: str
  class Change: path: str; mode: str; old: str | None; new: str | None    # new is None for a deleted path
  def base_commit(repo: Path, base_branch: str) -> str | None   # the full id of refs/remotes/origin/<b>, else refs/heads/<b>
  def is_commit(repo: Path, sha: str) -> bool
  def listing(repo: Path, commit: str) -> tuple[list[Entry], list[str]] | None    # entries, and notes for names left out
  def changes(repo: Path, old: str, new: str) -> tuple[list[Change], list[str]] | None   # None when git cannot diff them
  def read_blobs(repo: Path, blobs: Sequence[str], limit: int) -> Iterator[tuple[str, bytes | None]]   # None: missing or over `limit`

  # graph_build.py
  class BuildError(Exception): ...
  @dataclass(frozen=True)
  class Result:
      commit: str; previous: str | None; full: bool
      parsed: int; cached: int; resolved: int; removed: int
      left_out: dict[str, int]; languages: dict[str, str]; notes: tuple[str, ...]; ms: int
  def build(repo: Path, cfg: Config, *, full: bool = False, download: bool = True, commit: str | None = None) -> Result
  ```
- **Git.** `listing` is `git ls-tree -r -z --full-tree <commit>`; `changes` is `git diff-tree -r -z --raw --no-renames --no-ext-diff --no-textconv <old> <new>`; `read_blobs` is `git cat-file --batch-check` for the sizes, then `git cat-file --batch` for the blobs at or under `limit`. Every call: `["git", "--no-pager", *HARDENED, "-C", repo, …]`, environment plus `GIT_TERMINAL_PROMPT=0`, `GIT_NO_LAZY_FETCH=1`, `GIT_OPTIONAL_LOCKS=0`, a 60 s timeout, `stdin` closed or fed only ids. An id is used only when it matches `[0-9a-f]{40}|[0-9a-f]{64}`. A path that is not UTF-8, holds a control character, or is longer than 1,000 bytes is left out with one note per kind (counted, not listed). Nothing here raises: a failure is `None`.
- **`build`, in order:**
  1. Refuse with `BuildError` when `cfg.graph.enabled` is false, when FTS5 is missing, when no commit can be resolved, or when another build holds `<graph dir>/build.lock` (`fcntl.flock`, non-blocking).
  2. `conn, temp = graph_store.begin(path, fresh=full)`. The build is **full** when `full` is set (a repair: it starts from a new schema and keeps nothing, the facts cache included), or the store's `meta` has no `commit`.
  3. The change list: full → every `listing` entry as an added path, and every stored path missing from it as deleted. Otherwise `changes(previous, commit)`; when that is `None` (the old commit is gone), compare `listing(commit)` with the stored `files` rows by blob id and mode.
  4. `.gitignore`: for every changed path whose basename is `.gitignore`, read its blob (capped by `graph_ignore.MAX_FILE_BYTES`) and `set_ignore_file`. If any changed, or the build is full, take `listing(commit)` and judge **every** name again; a stored path whose state changes joins the change list as changed. Otherwise judge only the changed paths.
  5. For each changed path: deleted → `drop_file`. A state other than `indexed` → `drop_file`, then `put_file` with that state and no nodes. Otherwise the language is `graph_select.parses(path, doc_roots)`; a path with none, or over `cfg.graph.max_file_kb`, or whose first 8,000 bytes hold a NUL, is `unparsed` with a file node. The rest: `graph_store.facts(blob, lang, version)` or, on a miss, `Parser.parse` then `put_facts`. A file whose facts carry `no_grammar` is `unparsed`. Blobs are read in one `read_blobs` call. The `Parser` is started only when at least one blob must be parsed.
  6. A language whose stored `version` differs from `Parser.languages` is re-extracted for all its files, **only** when the parser offers that language. When it does not (offline, no cache), unchanged files keep their stored facts.
  7. `affected` = `graph_store.wanting(old provided keys | new provided keys)` over the changed paths, minus the changed and deleted paths. `set_keys` for each changed path; then `resolve` and `replace_graph` for each changed and each affected path, with an `Index` backed by the store.
  8. `set_meta(commit=…, built_at=…, languages=json)`, `prune_facts`, `publish`. On any exception: `discard`, release the lock, raise `BuildError` for an expected failure and re-raise anything else.
- `doc_roots` are the directory parts of `cfg.project.docs` values. `Result.left_out` counts the `files` rows by state other than `indexed`. `Result.languages` has one entry per language that at least one tracked file has: `"exact"` when the parser offered it, else `"not parsed"`. `Result.notes` holds at most 50, the rest counted in a last line.

- [ ] **Step 1: Write the failing tests**

`test_graph_git.py` (temp repositories from `tmp_git`): `base_commit` prefers `origin/<branch>` over the local branch and returns a 40-character id; a branch name `--upload-pack=x` returns None and runs nothing; `listing` returns the mode `120000` for a symlink and `160000` for a submodule entry; a file named `a\nb.py` or with a non-UTF-8 name is left out with a note; `changes` between two commits lists an add, a change, a delete and a mode change, and returns None for an unknown id; `read_blobs` returns None for a blob over `limit` without reading it and for a missing id; a `core.hooksPath` and a `core.fsmonitor` pointing at a script that writes a marker file leave no marker.

`test_graph_build.py`, with a helper `repo(tmp_path, files: dict[str, bytes])` that commits and `cfg` patched to `graph.enabled: true`:

```python
def test_first_build_then_no_change(repo_a, cfg):
    first = build(repo_a, cfg, download=False)
    assert first.full and first.previous is None and first.parsed == 6
    again = build(repo_a, cfg, download=False)
    assert (again.full, again.parsed, again.cached, again.resolved, again.removed) == (False, 0, 0, 0, 0)

def test_a_one_file_commit_parses_one_blob(repo_a, cfg, monkeypatch):
    build(repo_a, cfg, download=False)
    seen = spy_on_parser(monkeypatch)                      # records every (path, lang) handed to Parser.parse
    commit(repo_a, {"pkg/slots.py": b"def lease(): ...\ndef free(): ...\n"})
    result = build(repo_a, cfg, download=False)
    assert seen == [("pkg/slots.py", "python")] and result.parsed == 1
    assert result.resolved >= 1                             # pkg/cap.py imports it and is re-resolved, not re-parsed
```

- **Delta equals full.** For each of these edits applied after a first build, `graph_store.dump` of the delta-built store equals the dump of a store built `full=True` at the same commit in another state dir (ignoring `meta.built_at`): add a file; change a definition another file references; delete an imported file; rename a file (same blob) that another imports; revert the last commit; rewrite history (`git commit --amend`) so the old commit is unreachable; rewrite it and prune (`git gc --prune=now` after `git reflog expire --expire=now --all`) so `changes` returns None.
- **No re-parse.** A rename with the same blob parses nothing (`cached == 1`); a revert parses nothing.
- **`.gitignore`.** A repository whose `.gitignore` names `generated/` and which force-adds `generated/api.py` and `.env`: neither is handed to `read_blobs` (spy) nor has a node, and `left_out` counts `{"ignored": 1, "secret": 1}`. A commit adding `*.snap` to `.gitignore` removes `a.snap`'s nodes and parses nothing; a commit removing the pattern parses exactly that file. An uncommitted `.gitignore` edit and an uncommitted source edit change nothing. A `.git/info/exclude` entry and a global excludes file (`core.excludesFile`) change nothing. A nested `pkg/.gitignore` applies below `pkg/` only. A `graph.exclude` of `["docs/drafts/"]` gives state `excluded`.
- **Nothing runs.** A repository tracking `conftest.py`, `setup.py`, `sitecustomize.py` and a `package.json` with an `install` script, each written to create a marker file when executed: after a build no marker exists.
- **Limits.** A file over `max_file_kb` and a file with a NUL byte are `unparsed`, have a file node and were not parsed.
- **Refusals.** `graph.enabled: false` → `BuildError` naming `graph.enabled`. A second `build` while the lock is held → `BuildError("another build is running")`. A repository with no commit → `BuildError`.
- **Interrupted.** With `graph_store.publish` patched to raise `KeyboardInterrupt`, the store still answers `open_read` with the previous snapshot, and the next `build` succeeds and leaves no temp file.
- **Grammar missing.** With the parser forced to `stdlib` mode, a `.ts` file is `unparsed` with the note `no_grammar`; a later build with the grammar present parses it (the stored version differed).
- **TypeScript end to end** (uses Task 7's `exact` fixture; skipped without the cache, required in CI): `web/a.ts` importing `./b` gets an `imports` edge to `web/b.ts`.

- [ ] **Step 2: Run and see them fail**

- [ ] **Step 3: Implement `graph_git.py`, then `graph_build.py`**

- [ ] **Step 4: Run both test files, `test_portability.py` and ruff; all pass**

- [ ] **Step 5: Commit** — `Build the knowledge graph from git as a delta that parses only what changed.`

---

### Task 11: The `agentflow graph` commands, their text, and `doctor`

**Depends on:** Task 10

**Files:**
- Create: `agentflow/graph_report.py`
- Modify: `agentflow/flow.py` (a `graph` parser with `build`, `status`, `query`, `show` after the `workspace` parser at line 1464; `cmd_graph`)
- Modify: `agentflow/doctor.py` (`_graph_checks`, appended in `run_checks` before the gate checks, only when `cfg.graph.enabled`)
- Test: `agentflow/tests/test_graph_cli.py`, `agentflow/tests/test_doctor.py`

**Interfaces:**
- Consumes: `graph_build.build`, `Result`, `BuildError`; `graph_store` reads; `doctor.printable`, `doctor.Check`.
- Produces:
  ```python
  # graph_report.py (pure)
  def build_lines(result: Result) -> list[str]
  def status_lines(meta: Mapping[str, str], counts: Mapping[str, int], now: float) -> list[str]
  def query_lines(hits: Sequence[Hit], commit: str) -> list[str]
  def show_lines(target: Node, around: Sequence[tuple[Edge, Node]], commit: str) -> list[str]
  def as_json(...) -> dict     # one per view, the same data
  # flow.py
  def cmd_graph(args: argparse.Namespace) -> int
  ```
- Commands and flags: `graph build [--full]`; `graph status [--json]`; `graph query TEXT [--kind KIND] [--limit N] [--json]` (`KIND` from `NODE_KINDS`, `N` from 1 to 50, default 10); `graph show TARGET [--depth N] [--json]` (`TARGET` a node id or a path; `N` from 1 to 3, default 1). All take `--config`. None takes `--all` in K1.
- Exact lines:
  - build: `graph: <sha12>: parsed <n>, from cache <n>, re-resolved <n>, removed <n> (<ms> ms)`, then `languages: python exact · typescript exact · javascript not parsed (no grammar)`, then `left out: .gitignore <n> · secret <n> · vendored <n> · excluded <n> · other <n>` when any is above zero, then up to 20 notes, each `note: …`.
  - no index: `graph: no index yet: run \`agentflow graph build\`` and exit 1 (for `status`, `query` and `show`).
  - off: `error: graph.enabled is false: set it to true in .agentflow/config.yaml to build the index` and exit 1.
  - query: first `# index at <sha12>`, then one line per hit `<kind>  <path>:<line>  <name>` with ` — <text, 80 characters>` when it has text; no hit: `no match`, exit 0.
  - show: for a path with no node, `no node: <path> is left out (<state>)`; an unknown target, `no node: <target>`, exit 1.
- Doctor: `Check("graph search", "ok"|"fail", …)` for FTS5 (fix: "use a Python whose SQLite has FTS5"), and `Check("graph index", …)`: `ok` with `<sha12>, python exact, typescript exact`; `warn` with fix `agentflow graph build` when there is no index, or when a language of an indexed file is not parsed. It runs no command.

- [ ] **Step 1: Write the failing tests** (`capsys`, `monkeypatch.chdir` into a temp repository with a pack whose `graph.enabled` is true)

- `graph build` prints the first line in the format above and exits 0; a second run prints `parsed 0, from cache 0, re-resolved 0, removed 0`.
- With `graph.enabled: false`, `graph build` prints the `error:` line, exits 1 and creates no file under the state dir.
- `graph status` before any build prints the "no index yet" line and exits 1; after one it prints the commit, the counts and the languages; `--json` parses and holds `commit`, `files`, `nodes`, `edges`, `left_out`, `languages`.
- `graph query "run cap"` lists the node for `pkg/cap.py`; `--kind symbol` lists only symbols; `--limit 0` and `--limit 51` are argparse errors; a query of `'"* OR'` prints `no match`.
- `graph show pkg/cap.py` lists its symbols and its `imports` edges, each edge with its confidence; `graph show generated/api.py` (force-added and ignored) prints `no node: generated/api.py is left out (ignored)`.
- A tracked file named `a\x1b[31m.py`: no output line holds the escape byte.
- A store file replaced by random bytes: `graph status` prints the "no index yet" line and no traceback, and `graph build` rebuilds.
- `-C <repo> graph status` from another directory equals running it inside.
- Doctor: with `graph.enabled: false` no check named `graph …` exists and the recorded commands (`run_bounded` spy, as `test_doctor.py` line 339 does) are unchanged; with it true and no index, `graph index` is `warn`; after a build it is `ok`; the spy still sees no new command.

- [ ] **Step 2: Run and see them fail**

- [ ] **Step 3: Implement `graph_report.py`, `cmd_graph` and the parser, and `_graph_checks`**

- [ ] **Step 4: Run `test_graph_cli.py`, `test_doctor.py`, `test_explain_golden.py`, `test_portability.py` and ruff; all pass**

- [ ] **Step 5: Commit** — `Add agentflow graph build, status, query and show, and the doctor's graph checks.`

---

### Task 12: The retrieval measurement, the docs and the 3.19 release

**Depends on:** Task 11

**Files:**
- Create: `agentflow/tests/fixtures/graph/retrieval.yaml`, `agentflow/tests/test_graph_retrieval.py`
- Modify: `docs/agentflow-playbook.md` (§10; a new §18 "The knowledge graph"), `ARCHITECTURES.md` (§5 and §5.1), `CLAUDE.md` (Commands, Engine architecture, the dependency invariant), `CHANGELOG.md`, `pyproject.toml`, `agentflow/pack.py`

- [ ] **Step 1: Write `retrieval.yaml` before running any query.** A YAML list of exactly these 30 entries, each `{q: "<question>", file: "<path>"}`; they paraphrase `ARCHITECTURES.md` §5.1 and are frozen once written:

| # | q | file |
|---|---|---|
| 1 | append-only event log and the run lock | `agentflow/events.py` |
| 2 | reduce the event log into the run's state | `agentflow/state.py` |
| 3 | merge the config layers: engine defaults, starter, pack, user file | `agentflow/layers.py` |
| 4 | find the pack in the main checkout and check the engine version range | `agentflow/pack.py` |
| 5 | refuse a step that touches forbidden paths or moves HEAD | `agentflow/guards.py` |
| 6 | write files without following symlinks | `agentflow/safeio.py` |
| 7 | build the command line for the agent CLI and parse its stream | `agentflow/agents.py` |
| 8 | the phase loop that starts a run and advances it | `agentflow/pipeline.py` |
| 9 | decide which step of a phase runs next | `agentflow/flow_control.py` |
| 10 | the result file schema, the inbox and notifications | `agentflow/hitl.py` |
| 11 | pause, resume, cancel, retry and archive a run | `agentflow/run_control.py` |
| 12 | lease each live run a slot number for ports | `agentflow/slots.py` |
| 13 | hold a session past the machine-wide cap on active runs | `agentflow/run_cap.py` |
| 14 | hold runs on the five hour and seven day usage windows | `agentflow/governor.py` |
| 15 | keep the machine awake while a run advances | `agentflow/keepawake.py` |
| 16 | resolve the workspace roots into repositories | `agentflow/repo_set.py` |
| 17 | read a repository from its base branch to propose gates and a starter | `agentflow/detect.py` |
| 18 | check agent logins, the remote and docker on this machine | `agentflow/doctor.py` |
| 19 | the backlog claim protocol with leased pushes | `agentflow/claims.py` |
| 20 | parse and render the backlog file | `agentflow/backlog_file.py` |
| 21 | parse the lessons files and select lessons for a prompt | `agentflow/lessons.py` |
| 22 | turn a run's events into signals | `agentflow/signals.py` |
| 23 | audit the obligations at finish before the push | `agentflow/conformance.py` |
| 24 | render the run brief, the appended system prompt | `agentflow/brief.py` |
| 25 | named condition checks such as file exists and section items | `agentflow/conditions.py` |
| 26 | parse plan tasks and the capability map | `agentflow/sources.py` |
| 27 | number the doubt review's findings and apply its stop rules | `agentflow/doubt.py` |
| 28 | dependabot alert intake and the probe | `agentflow/vuln.py` |
| 29 | edit the config file from the settings page keeping comments | `agentflow/config_edit.py` |
| 30 | wait for CI, fix failures and mark the pull request ready | `agentflow/ci_lane.py` |

- [ ] **Step 2: Write the test**

```python
def test_retrieval_on_this_repository(repo_root, tmp_cfg):
    if not (repo_root / ".git").exists():
        pytest.skip("not a git checkout")
    cfg = load_config(tmp_cfg({"graph": {"enabled": True, "exclude": ["ARCHITECTURES.md", "CLAUDE.md"]}}))
    head = run_git(repo_root, "rev-parse", "HEAD").strip()
    build(repo_root, cfg, download=False, commit=head)
    conn = graph_store.open_read(graph_store.path_for(pack.state_dir(repo_root)))
    misses = []
    for case in yaml.safe_load(RETRIEVAL.read_text(encoding="utf-8")):
        paths = list(dict.fromkeys(h.node.path for h in graph_store.search(conn, case["q"], limit=50)))[:5]
        if case["file"] not in paths:
            misses.append((case["q"], paths))
    assert len(misses) <= 6, misses          # success criterion 1: at least 24 of 30
```

- [ ] **Step 3: Run it once and record the score.** Run: `uv run --no-project --with pyyaml --with pytest pytest agentflow/tests/test_graph_retrieval.py --import-mode=prepend -q`.
  - **24 or more:** go on.
  - **Fewer than 24:** change only general ranking (the `bm25` column weights, the `words` splitting, which fields are indexed). Never change a question, an expected file, or add a rule that names a file or a term from the list. If it is still under 24, **stop**: do not finish the slice, and report the score and the misses. The spec says to revisit the no-embeddings assumption before K2.
  - Write the first score and the final score in the pull request body, with the misses.

- [ ] **Step 4: Measure the build times on this repository** with a copy of the pack that sets `graph.enabled: true` (`--config`), and record them in the pull request: a first `agentflow graph build` (target: under 20 s), a build with no new commit (`parsed 0`), and a build after a one-file commit on a scratch branch (target: `parsed 1`, under 2 s). A missed target is reported, not hidden; it does not block the slice unless the delta build parses more than the changed file.

- [ ] **Step 5: Write the docs** (if building showed a deviation from the proposal, also add a dated "As built in K1" note to the proposal; otherwise leave it alone)
  - Playbook §10: the four `agentflow graph` commands. §18 "The knowledge graph": turning it on, what is indexed and what is left out (`.gitignore`, secrets, the fixed list, `graph.exclude`), that a build never fetches and reads only committed files, what a re-index does, the worker and its one-time download, what is exact and what is heuristic, the residual risk of parsing a repository you do not trust (point to §11), and troubleshooting (`no index yet`, `no grammar`, `another build is running`).
  - `ARCHITECTURES.md` §5: the graph modules in the module map and one row each in §5.1.
  - `CLAUDE.md`: the commands; one paragraph under "Engine architecture" for the graph modules; and under "Invariants" the sentence that `graph_worker.py` is a separate `uv` script whose pinned packages the engine process never imports, and that its pins and lock change only in a reviewed change.

- [ ] **Step 6: Release 3.19.** Set `version = "3.19"` in `pyproject.toml` and `ENGINE_VERSION = "3.19"` in `agentflow/pack.py`; rename `## [Unreleased]` to `## [3.19] - <today>` with a fresh empty `## [Unreleased]` above it. If `main` already holds 3.19, take the next free number. The entry, under `### Added`: the `agentflow graph` commands and what they index; the three `graph.*` keys; the parser worker, its three pinned packages and that only `agentflow graph build` may download them; the `doctor` lines.

- [ ] **Step 7: Run everything.** `test_packaging.py`, `test_portability.py`, `test_explain_golden.py`, every `test_graph_*.py`, then the full suite (`uv run --no-project --with pyyaml --with pytest pytest agentflow/tests --import-mode=prepend -q`, 25 to 60 minutes), `uvx ruff@0.17.4 check .` (`All checks passed!`) and `bin/agentflow validate` (`0 warnings`).

- [ ] **Step 8: Commit** — `Measure retrieval on this repository, document the knowledge graph, and release 3.19.`

---

## After the last task

- Review with the `security-auditor` agent beside the branch review. Point it at Tasks 6, 7 and 10 and at the threat model above.
- The pull request body carries: the retrieval score (first and final) with the misses; the three build timings; the output of Task 7 Step 5 (the lock refusing a changed pin); and whether CI ran the tree-sitter tests (they must not be skipped there, which is the first real run of the grammars on Linux).
