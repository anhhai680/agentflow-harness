"""Detect a repository's base branch, starter, gates and docs: the facts `agentflow init` proposes to a human.

The repository is untrusted input. Nothing in it is run, imported or followed: files are read as text from the base
branch with `git show`, `git cat-file -e` and `git ls-tree` (the working tree is read only when the repository has no
commit yet), parsed with `yaml.safe_load`, `json` and `tomllib`, and a file that is malformed, too large or not a shape
this module knows becomes a note and never an exception. Everything detected is a proposal; the caller writes it.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import stat
import subprocess
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agentflow import layers

MAX_BYTES = 1024 * 1024  # a larger file is not read
GIT_TIMEOUT_S = 30
WORKFLOWS = ".github/workflows"
GUESSED = "guessed the base branch from the current branch: check `project.base_branch`"
_BRANCH = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/+@-]*")  # a name that can't be read as an option or a path escape
_INSTALL = re.compile(
    r"^\s*(sudo\s+)?((python[0-9.]*\s+-m\s+)?(pip|pip3)|uv|npm|pnpm|yarn|npx|corepack|poetry|pipenv|bundle|apt-get|apt"
    r"|brew|go)(\s+(install|i|sync|ci|add|update|enable|get|mod\s+download)\b|\s*$)")
_PIPED_INTO_SHELL = re.compile(r"\|\s*(sudo\s+)?(ba)?sh\b")
_GATE_WORDS = re.compile(r"\b(test|tests|pytest|unittest|jest|vitest|tox|lint|ruff|eslint|flake8|mypy|pyright|tsc"
                         r"|typecheck|check|vet|clippy)\b", re.IGNORECASE)
_FAST_WORDS = re.compile(r"\b(lint|ruff|eslint|flake8|mypy|pyright|tsc|typecheck|vet|clippy)\b", re.IGNORECASE)
_GITHUB_REFERENCE = re.compile(r"\$\{\{|\$\{?GITHUB_")
_REWRITES = re.compile(r"(?<![\w-])--(write|fix)\b")
_SHELL_SYNTAX = re.compile(r"[|$><`(;&]")
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MAKE_TARGET = re.compile(r"^(test|lint|check)\s*:(?!=)", re.MULTILINE)
_SCRIPTS = ("test", "lint", "typecheck", "check")
_SPEC_DIR = "spec" + "s"  # spelled in two parts: the portability scan refuses the project's own path in engine code
_DOC_DIRS = {"specs": (_SPEC_DIR, "docs/rfcs", "docs/proposals"), "plans": ("docs/plans", "plans"),
             "decisions": ("docs/adr", "docs/decisions")}


@dataclass
class Detected:
    base_branch: str
    base_ref: str | None  # what the files were read from; None for a repository with no commit, read from its tree
    starter: str
    gates: dict[str, dict] = field(default_factory=dict)  # written whole into the pack; empty when the starter's stand
    gate_sets: dict[str, list[str]] = field(default_factory=dict)
    gate_source: str = "starter"  # "ci", "scripts" or "starter"
    docs: dict[str, str] = field(default_factory=dict)
    skills: list[str] = field(default_factory=list)
    agent_docs: list[str] = field(default_factory=list)  # printed only
    notes: list[str] = field(default_factory=list)


# --- reading the repository -----------------------------------------------------------------------------------------

def _git(repo: Path, *args: str, limit: int | None = None) -> tuple[int, bytes]:
    """Run `git -C repo args`: (exit code, stdout), or (1, b"") when git can't run. Never raises. With `limit`, at
    most `limit + 1` bytes are read and the process is stopped, so a huge blob isn't loaded."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat"}
    try:
        proc = subprocess.Popen(["git", "--no-pager", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    except (OSError, ValueError):
        return 1, b""
    timer = threading.Timer(GIT_TIMEOUT_S, proc.kill)
    timer.start()
    try:
        assert proc.stdout is not None
        data = proc.stdout.read() if limit is None else proc.stdout.read(limit + 1)
        if limit is not None and len(data) > limit:
            proc.kill()
        proc.stdout.close()
        return proc.wait(), data
    except OSError:
        proc.kill()
        return 1, b""
    finally:
        timer.cancel()


def _git_text(repo: Path, *args: str) -> str | None:
    code, out = _git(repo, *args)
    return out.decode("utf-8", "replace").strip() if code == 0 else None


class _Source:
    """The files of the repository, from `ref`, or from the working tree when `ref` is None (no commit yet)."""

    def __init__(self, repo: Path, ref: str | None, notes: list[str]) -> None:
        self.repo, self.ref, self.notes = repo, ref, notes

    def read(self, path: str) -> str | None:
        """The text of the file at `path`; None when it is missing, not a file, too large or not UTF-8 (the last two
        leave a note)."""
        if self.ref is None:
            data = self._read_tree(path)
        else:
            code, data = _git(self.repo, "show", f"{self.ref}:{path}", limit=MAX_BYTES)
            data = data if code == 0 or len(data) > MAX_BYTES else None
        if data is None:
            return None
        if len(data) > MAX_BYTES:
            self.notes.append(f"{path} is over 1 MB: not read")
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            self.notes.append(f"{path} is not UTF-8 text: not read")
            return None

    def exists(self, path: str) -> bool:
        if self.ref is None:
            return self._inside(path) is not None
        return _git(self.repo, "cat-file", "-e", f"{self.ref}:{path}")[0] == 0

    def ls(self, directory: str) -> list[str]:
        """The names (not paths) inside `directory`, sorted; empty when it is missing."""
        if self.ref is None:
            real = self._inside(directory)
            try:
                return sorted(os.listdir(real)) if real and os.path.isdir(real) else []
            except OSError:
                return []
        code, out = _git(self.repo, "ls-tree", "--name-only", "-z", self.ref, "--", f"{directory}/")
        if code != 0:
            return []
        return sorted(posixpath.basename(p) for p in out.decode("utf-8", "replace").split("\0") if p)

    def _inside(self, path: str) -> str | None:
        """The real path of `path` in the working tree when it stays inside the repository (no link leads out)."""
        root = os.path.realpath(self.repo)
        real = os.path.realpath(os.path.join(root, path))
        return real if (real == root or real.startswith(root + os.sep)) and os.path.lexists(real) else None

    def _read_tree(self, path: str) -> bytes | None:
        real = self._inside(path)
        try:
            if real is None or not stat.S_ISREG(os.stat(real).st_mode):
                return None
            with open(real, "rb") as handle:
                return handle.read(MAX_BYTES + 1)
        except OSError:
            return None


# --- the base branch ------------------------------------------------------------------------------------------------

def _valid(branch: str | None) -> bool:
    return bool(branch) and _BRANCH.fullmatch(branch) is not None and ".." not in branch


def _local(repo: Path, branch: str) -> bool:
    return _valid(branch) and _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")[0] == 0


def _remote(repo: Path, branch: str) -> bool:
    return _valid(branch) and _git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}")[0] == 0


def default_branch(repo: Path) -> tuple[str, list[str]]:
    """The repository's base branch and the notes about how it was found: `origin/HEAD`, else a local `main` or
    `master`, else `init.defaultBranch` when it exists, else the current branch, else (a detached HEAD) `main`."""
    notes: list[str] = []
    branch = (_git_text(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD") or "").removeprefix("origin/")
    if not (_valid(branch) and (_local(repo, branch) or _remote(repo, branch))):
        branch = next((b for b in ("main", "master") if _local(repo, b)), "")
    if not branch:
        configured = _git_text(repo, "config", "init.defaultBranch") or ""
        branch = configured if _local(repo, configured) else ""
    if not branch:
        current = _git_text(repo, "symbolic-ref", "--short", "HEAD")
        branch = current if current and _valid(current) else "main"
        notes.append(GUESSED)
    elif not _local(repo, branch) and _remote(repo, branch):
        notes.append(f"create it locally: `git branch {branch} origin/{branch}`")
    return branch, notes


def _base_ref(repo: Path, branch: str, notes: list[str]) -> str | None:
    if _git(repo, "rev-parse", "--verify", "--quiet", "HEAD")[0] != 0:
        notes.append("no commits yet: files are read from the working tree, not from a base branch")
        return None
    if _local(repo, branch):
        return branch
    if _remote(repo, branch):
        return f"origin/{branch}"
    notes.append(f"no branch `{branch}` to read from: files are read from HEAD")
    return "HEAD"


# --- the starter ----------------------------------------------------------------------------------------------------

def _pick_starter(src: _Source, notes: list[str]) -> str:
    manifests = [(f, s) for f, s in (("pyproject.toml", "python-uv"), ("package.json", "node-pnpm"), ("go.mod", "go"))
                 if src.exists(f)]
    names = layers.starter_names()
    if not manifests:
        if src.exists("Cargo.toml"):
            notes.append("no Rust starter yet: using docs-only")
        return "docs-only"
    starter = manifests[0][1] if manifests[0][1] in names else "docs-only"
    if len(manifests) > 1:
        found = [f for f, _ in manifests]
        listed = " and ".join([", ".join(found[:-1]), found[-1]] if len(found) > 2 else found)
        notes.append(f"found {listed}; picked {starter}: pass `--starter` to choose")
    return starter


def _starter_gates(starter: str) -> dict[str, dict]:
    gates: dict[str, dict] = {}
    for layer in layers.base_layers(starter):
        section = layer.data.get("gates")
        if isinstance(section, dict):
            gates.update({str(k): v for k, v in section.items() if isinstance(v, dict)})
    return gates


def _node_manager(src: _Source) -> str:
    if src.exists("pnpm-lock.yaml"):
        return "pnpm"
    return "yarn" if src.exists("yarn.lock") else "npm"


def _setup_overrides(src: _Source, starter: str, notes: list[str]) -> dict[str, dict]:
    """The setup gates that make the starter's setup match the project's lockfiles."""
    if starter == "node-pnpm" and not src.exists("pnpm-lock.yaml"):
        if src.exists("yarn.lock"):
            return {"install": {"run": "yarn install --frozen-lockfile"}}
        return {"install": {"run": "npm ci" if src.exists("package-lock.json") else "npm install"}}
    if starter == "python-uv" and not src.exists("uv.lock"):
        if src.exists("poetry.lock"):
            return {"sync": {"run": "poetry install"}}
        notes.append("the python-uv starter runs `uv sync`: override `gates.sync` if this project doesn't use uv")
    return {}


# --- gates ----------------------------------------------------------------------------------------------------------

class _Gates:
    """The detected gates in order: duplicates collapse and a repeated name gets a numeric suffix."""

    def __init__(self, reserved: set[str]) -> None:
        self.items: dict[str, dict] = {}
        self._seen: set[tuple[str, str | None]] = set()
        self._taken = set(reserved)

    def add(self, name: str, run: str, cwd: str | None) -> None:
        if (run, cwd) in self._seen:
            return
        self._seen.add((run, cwd))
        unique, n = name, 1
        while unique in self._taken:
            n += 1
            unique = f"{name}_{n}"
        self._taken.add(unique)
        self.items[unique] = {"run": run, **({"cwd": cwd} if cwd else {})}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower())[:30].strip("_")


class _Skip(Exception):
    """A file, job or step that can't become a gate; the message is its note."""


def _mapping(owner: dict, key: str, what: str) -> dict:
    value = owner.get(key, {})
    if not isinstance(value, dict):
        raise _Skip(f"`{what}` is not a mapping")
    return value


def _text_or_absent(owner: dict, key: str, what: str) -> str | None:
    value = owner.get(key)
    if value is not None and not isinstance(value, str):
        raise _Skip(f"`{what}` is not text")
    return value


def _env_of(owner: dict, what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in _mapping(owner, "env", what).items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        if value is None:
            value = ""
        if not isinstance(key, str) or not _ENV_KEY.fullmatch(key):
            raise _Skip(f"`{what}` has a name that isn't a shell variable: {key!r}")
        if not isinstance(value, (str, int, float)):
            raise _Skip(f"`{what}.{key}` is not a plain value")
        out[key] = str(value)
    return out


def _run_defaults(owner: dict, what: str) -> dict:
    defaults = _mapping(owner, "defaults", f"{what}defaults")
    run = _mapping(defaults, "run", f"{what}defaults.run")
    _text_or_absent(run, "shell", f"{what}defaults.run.shell")
    _text_or_absent(run, "working-directory", f"{what}defaults.run.working-directory")
    return run


def _triggers(on: Any) -> bool | None:
    """Whether a workflow's `on:` is a CI trigger (a pull request, or a push that isn't tags only); None when it is
    not a value GitHub accepts."""
    if isinstance(on, str):
        return on in ("push", "pull_request")
    if isinstance(on, list):
        return any(x in ("push", "pull_request") for x in on if isinstance(x, str))
    if not isinstance(on, dict):
        return None
    if "pull_request" in on:
        return True
    if "push" not in on:
        return False
    push = on["push"]
    tags_only = (isinstance(push, dict) and ("tags" in push or "tags-ignore" in push)
                 and not ("branches" in push or "paths" in push))
    return not tags_only


def _segments(line: str) -> list[str]:
    """Split a command line at `&&` and `;` outside quotes and parentheses. An unbalanced line stays in one piece."""
    parts: list[str] = []
    buf: list[str] = []
    quote, depth, i = "", 0, 0
    while i < len(line):
        c = line[i]
        if quote:
            buf.append(c)
            if c == "\\" and quote == '"' and i + 1 < len(line):
                i += 1
                buf.append(line[i])
            elif c == quote:
                quote = ""
        elif c == "\\" and i + 1 < len(line):
            buf.append(c + line[i + 1])
            i += 1
        elif c in "'\"`":
            quote = c
            buf.append(c)
        elif c in "()":
            depth += 1 if c == "(" else -1
            buf.append(c)
        elif depth == 0 and (c == ";" or line.startswith("&&", i)):
            parts.append("".join(buf))
            buf = []
            i += 1 if c == "&" else 0
        else:
            buf.append(c)
        i += 1
    if quote or depth:
        return [line.strip()]
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _installs(segment: str) -> bool:
    return bool(_INSTALL.match(segment) or _PIPED_INTO_SHELL.search(segment))


def _without_installs(run: str) -> str | None:
    """`run` with its install commands removed; None when it mixes install and test lines in a script, "" when
    nothing remains."""
    lines = [ln for ln in run.splitlines() if ln.strip()]
    if len(lines) > 1:
        if any(_installs(seg) for ln in lines for seg in _segments(ln)):
            return None
        return run.strip("\n").rstrip()
    return " && ".join(seg for seg in _segments(run.strip()) if not _installs(seg))


def _command(remaining: str, shell: str | None, env: dict[str, str]) -> str:
    """The gate command with GitHub's semantics: the bare `run` when it is plain, else a shell invocation of the
    script (`CI=true`, the env, the commands)."""
    if "\n" not in remaining and not env and not _SHELL_SYNTAX.search(remaining):
        return remaining
    script = "\n".join(["export CI=true", *(f"export {k}={shlex.quote(v)}" for k, v in env.items()), remaining])
    prefix = {"bash": "bash --noprofile --norc -eo pipefail", "sh": "sh -e"}.get(shell or "", "bash -e")
    return f"{prefix} -c {shlex.quote(script)}"


def _step_gate(label: str, step: dict, env: dict[str, str], shell: str | None, working_dir: str | None,
               ) -> tuple[str, str, str | None] | None:
    """(name, command, cwd) when the step is a gate, None when it is no gate; `_Skip` when it can't run as one."""
    run = step["run"]
    if "\0" in run:
        raise _Skip("its `run` holds a NUL byte")
    if _GITHUB_REFERENCE.search(" ".join([run, working_dir or "", *env.values()])):
        raise _Skip("uses a GitHub expression or `GITHUB_` variable in `run`, `working-directory` or `env`")
    if _REWRITES.search(run):
        raise _Skip("its `run` rewrites files (`--write` or `--fix`)")
    cwd = None
    if working_dir:
        parts = working_dir.split("/")
        if working_dir.startswith(("/", "~")) or "\\" in working_dir or ".." in parts or "\0" in working_dir:
            raise _Skip(f"its `working-directory` ({working_dir}) is not a path inside the repository")
        cwd = posixpath.normpath(working_dir)
        cwd = None if cwd == "." else cwd
    remaining = _without_installs(run)
    if remaining is None:
        raise _Skip("mixes install and test commands: add it by hand")
    if not _GATE_WORDS.search(remaining):
        return None
    name = _slug(str(step.get("name") or "")) or _slug(" ".join(remaining.split()[:2])) or "gate"
    return name, _command(remaining, shell, env), cwd


def _job_gates(wf_name: str, job_id: str, job: Any, wf_env: dict[str, str], wf_run: dict, notes: list[str],
               gates: _Gates) -> None:
    where = f"{wf_name}/{job_id}"
    if not isinstance(job, dict):
        raise _Skip("is not a mapping")
    for key in ("if", "services", "container"):
        if key in job:
            raise _Skip(f"has `{key}:`, so its steps can't run as gates")
    strategy = job.get("strategy")
    if isinstance(strategy, dict) and "matrix" in strategy:
        raise _Skip("is a matrix job: a gate runs its steps once, not once per matrix value")
    steps = job.get("steps", [])
    if not isinstance(steps, list):
        raise _Skip("`steps` is not a list")
    job_run = _run_defaults(job, "")
    job_env = _env_of(job, "env")
    for number, step in enumerate(steps, 1):
        label = f"{where}/{step.get('name') if isinstance(step, dict) and isinstance(step.get('name'), str) else number}"
        try:
            found = _one_step(step, wf_env, job_env, wf_run, job_run)
        except _Skip as skip:
            notes.append(f"{label}: skipped, {skip}")
            continue
        if found:
            gates.add(*found)


def _one_step(step: Any, wf_env: dict, job_env: dict, wf_run: dict, job_run: dict) -> tuple[str, str, str | None] | None:
    if not isinstance(step, dict):
        raise _Skip("the step is not a mapping")
    if "uses" in step:
        raise _Skip("it uses an action, which can't run as a gate")
    run = step.get("run")
    if run is None:
        _text_or_absent(step, "name", "name")
        return None
    if not isinstance(run, str):
        raise _Skip("`run` is not text")
    name = _text_or_absent(step, "name", "name")
    shell = _text_or_absent(step, "shell", "shell") or job_run.get("shell") or wf_run.get("shell")
    working_dir = (_text_or_absent(step, "working-directory", "working-directory")
                   or job_run.get("working-directory") or wf_run.get("working-directory"))
    step_env = _env_of(step, "env")
    for key in ("if", "continue-on-error"):
        if key in step:
            raise _Skip(f"it has `{key}`")
    if shell not in (None, "bash", "sh"):
        raise _Skip(f"it uses `shell: {shell}`")
    return _step_gate(name or "", step, {**wf_env, **job_env, **step_env}, shell, working_dir)


def _workflow_gates(name: str, text: str, notes: list[str], gates: _Gates) -> None:
    try:
        wf = yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError, ValueError) as exc:
        notes.append(f"{name}: skipped, not valid YAML ({str(exc).splitlines()[0] if str(exc) else 'unreadable'})")
        return
    try:
        if not isinstance(wf, dict):
            raise _Skip("it is empty or not a mapping")
        counts = _triggers(wf.get("on", wf.get(True)))
        if counts is None:
            raise _Skip("its `on:` is not a string, a list or a mapping")
        jobs = wf.get("jobs")
        if not isinstance(jobs, dict):
            raise _Skip("`jobs` is not a mapping")
        wf_run = _run_defaults(wf, "")
        wf_env = _env_of(wf, "env")
    except _Skip as skip:
        notes.append(f"{name}: skipped, {skip}")
        return
    if not counts:
        return
    for job_id, job in jobs.items():
        try:
            _job_gates(name, str(job_id), job, wf_env, wf_run, notes, gates)
        except _Skip as skip:
            notes.append(f"{name}/{job_id}: skipped, {skip}")


def _ci_gates(src: _Source, notes: list[str], gates: _Gates) -> None:
    files = [n for n in src.ls(WORKFLOWS) if n.endswith((".yml", ".yaml"))]
    for name in files:
        text = src.read(f"{WORKFLOWS}/{name}")
        if text is not None:
            _workflow_gates(name, text, notes, gates)


def _script_gates(src: _Source, notes: list[str], propose) -> None:
    """The gates the project's own scripts name; `propose(name, run)` adds one."""
    text = src.read("package.json")
    if text is not None:
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            data = None
            notes.append("package.json: not valid JSON, so its scripts are not read")
        scripts = data.get("scripts") if isinstance(data, dict) else None
        manager = _node_manager(src)
        for script in _SCRIPTS:
            body = scripts.get(script) if isinstance(scripts, dict) else None
            if not isinstance(body, str):
                continue
            if "no test specified" in body:
                notes.append(f"package.json: the `{script}` script is the `npm init` placeholder: skipped")
                continue
            propose(script, {"pnpm": f"pnpm run {script}", "yarn": f"yarn {script}"}.get(manager, f"npm run {script}"))
    text = src.read("pyproject.toml")
    if text is not None or src.exists("tests"):
        tool: dict = {}
        try:
            parsed = tomllib.loads(text or "").get("tool", {})
            tool = parsed if isinstance(parsed, dict) else {}
        except (tomllib.TOMLDecodeError, RecursionError, ValueError):
            notes.append("pyproject.toml: not valid TOML, so its tool sections are not read")
        pytest_section = tool.get("pytest")
        if (isinstance(pytest_section, dict) and "ini_options" in pytest_section) or src.ls("tests"):
            propose("pytest", "uv run pytest -q")
        if "ruff" in tool:
            propose("ruff", "uv run ruff check .")
        if "mypy" in tool:
            propose("mypy", "uv run mypy .")
    text = src.read("Makefile")
    if text is not None:
        for target in dict.fromkeys(m.group(1) for m in _MAKE_TARGET.finditer(text)):
            propose(target, f"make {target}")


def _gate_sets(names: list[str], gates: dict[str, dict], notes: list[str]) -> dict[str, list[str]]:
    build = [n for n in names if _FAST_WORDS.search(n) or _FAST_WORDS.search(gates.get(n, {}).get("run", ""))]
    if not build:
        build = list(names)
        notes.append("no fast gate found: build runs the full verify set")
    return {"verify": list(names), "build": build}


# --- docs and skills ------------------------------------------------------------------------------------------------

def _docs(src: _Source, notes: list[str]) -> dict[str, str]:
    docs = {key: found for key, options in _DOC_DIRS.items() if (found := next((d for d in options if src.ls(d)), ""))}
    if _SPEC_DIR in src.ls("docs"):
        notes.append("set `project.docs.specs` to that directory")
    return docs


# --- the entry point ------------------------------------------------------------------------------------------------

def detect(repo: Path, starter: str | None = None) -> Detected:
    """Read `repo`'s base branch and propose its starter, gates, docs and skills.

    `starter` (a name from `layers.starter_names()`, else `ValueError`) overrides the one picked from the manifests.
    Nothing here raises for a malformed file: it becomes a note."""
    repo = Path(repo)
    if starter is not None:
        layers.starter_file(starter)  # raises LayerError, a ValueError, for a name the engine doesn't ship
    branch, notes = default_branch(repo)
    ref = _base_ref(repo, branch, notes)
    src = _Source(repo, ref, notes)
    picked = starter if starter is not None else _pick_starter(src, notes)
    overrides = _setup_overrides(src, picked, notes)
    found = _Gates(set(overrides))
    source = "starter"
    standing: list[str] = []
    _ci_gates(src, notes, found)
    if found.items:
        source = "ci"
    else:
        starter_gates = _starter_gates(picked)

        def propose(name: str, run: str) -> None:
            same = next((n for n, g in starter_gates.items() if g.get("run") == run), None)
            if same is None:
                found.add(name, run, None)
            elif same not in standing:
                standing.append(same)  # the starter's gate already runs it, so that gate stands

        _script_gates(src, notes, propose)
        source = "scripts" if found.items else "starter"
    gates = {**overrides, **found.items}
    gate_sets = _gate_sets([*standing, *found.items], {**_starter_gates(picked), **gates}, notes) if found.items else {}
    return Detected(
        base_branch=branch, base_ref=ref, starter=picked, gates=gates, gate_sets=gate_sets, gate_source=source,
        docs=_docs(src, notes),
        skills=[n for n in src.ls(".claude/skills") if src.exists(f".claude/skills/{n}/SKILL.md")],
        agent_docs=[n for n in ("CLAUDE.md", "AGENTS.md") if src.exists(n)], notes=notes)
