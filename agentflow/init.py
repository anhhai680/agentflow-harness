"""`agentflow init`: write a sparse pack for a repository, and run its gates once on the base branch only when asked.

`init` detects (`detect.detect`, which reads the base branch through git and runs nothing), renders a provisional pack
into the state dir so `doctor` has a config to check, and writes `.agentflow/config.yaml` exactly once, exclusively
(`safeio.write_new`): it never overwrites anything, and a pack that appears meanwhile wins. Nothing the repository
holds runs unless the human passes `--baseline`.

The baseline runs the gates in a throwaway worktree of the base branch, under `<state_dir>/baseline-*`, with the
caller's virtualenv left out of the environment. Only a plain non-zero exit makes a gate `fail` (red); a missing
program, exit 126 or 127, an exit of 128 or more (a signal), a runner that says it could not start a command, a
timeout, a missing `cwd` or a failed setup is an environment problem, never red. One baseline runs at a time per
repository (`<state_dir>/init.lock`). The
worktree goes in a `finally`, also on a timeout and on SIGTERM or SIGHUP (they raise `KeyboardInterrupt` while the
baseline runs), and an interrupted baseline writes no pack. Cleanup touches only init's own `baseline-*` worktrees.
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TextIO

import yaml

from agentflow import detect, doctor, layers, pack, safeio
from agentflow.config import Config, ConfigError, Gate
from agentflow.detect import Detected
from agentflow.gates import GateResult, resolve, run_gates

_show = doctor.printable

ENGINE_RANGE = ">=2,<3"
GIT_TIMEOUT_S = 120
DROPPED_ENV = ("VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH")  # the caller's Python environment, not the project's
GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")  # they would point git at the caller's repository, not the worktree
ALREADY = "this repository already has a pack: edit it, or remove .agentflow to start over"
SYMLINKED = f"{pack.PACK_DIR} is a symlink: init writes the pack only into a real folder"
PERMISSION_COMMENT = ("runs are headless: this lets the agent use its tools; deny rules, hooks and agentflow's guards "
                      "still bind. Without it, each gate and the bug lane's test command need a Bash allow rule in "
                      ".claude/settings.json (validate lists them).")
HEADER = """\
# agentflow pack, written by `agentflow init`. A setting not written here comes from the engine defaults or the
# starter: `agentflow config show --effective` lists every value and the layer it comes from.
# The gates are a proposal read from the repository: check `gates` and `gate_sets`, then set `gates_confirmed: true`.
"""
_GATES_FROM = {"ci": "read from the CI workflows", "scripts": "read from the project's scripts",
               "starter": "the project's own setup; the starter supplies the rest"}
_REF = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/+@-]*")  # a ref that can't be read as an option
_EXIT = re.compile(r"exit (-?\d+)")
_SUBCOMMAND = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]*")
_PROGRAM = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*")  # a bare name: never a path, a quote or a backslash
_FLAG = re.compile(r"--?[A-Za-z0-9][A-Za-z0-9_.:=+@/-]*")
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_SHELLS = frozenset("bash sh zsh dash ksh env sudo xargs nohup exec eval".split())
_INTERPRETER = re.compile(r"(python|pypy)[0-9.]*|node|nodejs|ruby|perl|php|deno|bun|java|lua|Rscript")
_DISPATCHERS = frozenset("git uv npm pnpm yarn npx go cargo poetry pip pip3 pipenv docker make bundle corepack dotnet "
                         "gradle mvn rake mix hatch pdm".split())  # what they run is their subcommand
_FLAG_ARGS = {"git": ("-c", "-C"), "uv": ("--directory", "--project", "--python", "-p"), "make": ("-C", "-f"),
              "npm": ("--prefix",), "pnpm": ("-C", "--dir", "--filter", "-F"), "yarn": ("--cwd",), "go": ("-C",)}
_COULD_NOT_START = ("Failed to spawn", "command not found", ": not found")  # a runner that could not start a tool
_COMMAND_WIDTH = 60

Status = Literal["pass", "fail", "error", "skipped", "not run"]


@dataclass
class BaselineRow:
    gate: str
    command: str
    status: Status
    seconds: float
    note: str = ""


# --- rendering the pack ---------------------------------------------------------------------------------------------

class _Dumper(yaml.SafeDumper):
    """`yaml.safe_dump`'s dumper, writing a multi-line string as a literal block and never an alias."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|" if "\n" in data else None)


_Dumper.add_representer(str, _represent_str)


def _dump(block: dict) -> str:
    return yaml.dump(block, Dumper=_Dumper, sort_keys=False, default_flow_style=False, allow_unicode=True, width=4096)


def base_of(starter: str) -> dict:
    """The engine defaults and `starter` merged: what the pack is compared against and written on top of."""
    return layers.merge(layers.base_layers(starter))[0]


def _reference(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("@")


def _checked(d: Detected) -> tuple[dict[str, dict], dict[str, list[str]], dict[str, str], list[str]]:
    """`d`'s gates, gate sets and docs without any value the pack would read as an `@` reference, and a note for each
    one dropped. A set left empty is not written, so the starter's stands."""
    notes: list[str] = []
    gates: dict[str, dict] = {}
    for name, gate in d.gates.items():
        bad = next((key for key, value in gate.items() if _reference(value)), None)
        if bad is None:
            gates[name] = dict(gate)
        else:
            notes.append(f"gate `{name}` dropped: its `{bad}` starts with `@`, which a pack reads as a reference")
    sets: dict[str, list[str]] = {}
    for set_name, members in d.gate_sets.items():
        kept = [m for m in members if m in gates or m not in d.gates]
        if kept:
            sets[set_name] = kept
        elif members:
            notes.append(f"`gate_sets.{set_name}` is the starter's: every gate detected for it was dropped")
    docs: dict[str, str] = {}
    for key, value in d.docs.items():
        if _reference(value):
            notes.append(f"`project.docs.{key}` dropped: it starts with `@`, which a pack reads as a reference")
        else:
            docs[key] = value
    return gates, sets, docs, notes


def _red_sets(sets: dict[str, list[str]], base: dict, red: frozenset[str]) -> dict[str, list[str]]:
    """Every gate set, whole, without the red gates; `verify` keeps them when nothing else would be left."""
    below = base.get("gate_sets") if isinstance(base.get("gate_sets"), dict) else {}
    merged = {**below, **sets}
    out: dict[str, list[str]] = {}
    for name in dict.fromkeys(["setup", "build", "verify", *merged]):
        members = merged.get(name) if isinstance(merged.get(name), list) else []
        out[name] = [m for m in members if m not in red]
    if not out["verify"]:
        out["verify"] = list(merged.get("verify") or [])
    return out


def pack_data(d: Detected, *, base: dict, red: frozenset[str] = frozenset()) -> dict:
    """The data `render_pack` writes: what the pack states on top of `base` (`base_of(d.starter)`)."""
    gates, sets, docs, _notes = _checked(d)
    data: dict[str, Any] = {"version": pack.PACK_VERSION, "engine": ENGINE_RANGE, "starter": d.starter,
                            "gates_confirmed": False, "agents": {"claude": {"permission_mode": "bypassPermissions"}}}
    below = base.get("project") if isinstance(base.get("project"), dict) else {}
    project: dict[str, Any] = {key: d.base_branch for key in ("base_branch", "fork_from") if below.get(key) != d.base_branch}
    below_docs = below.get("docs") if isinstance(below.get("docs"), dict) else {}
    changed_docs = {key: value for key, value in docs.items() if below_docs.get(key) != value}
    if changed_docs:
        project["docs"] = changed_docs
    if project:
        data["project"] = project
    if "specs" in docs:  # fixed data only: nothing from the repository is copied into these two keys
        data["work_source"] = {"kind": "specs"}
        data["dashboard"] = {"backlog": {"module_lane": "feature",
                                         "new_task": {"feature": ["new_module", "issue", "after"],
                                                      "bug": ["issue", "task", "after"]}}}
    base_gates = base.get("gates") if isinstance(base.get("gates"), dict) else {}
    for name in [*gates, *(n for n in base_gates if n not in gates)]:
        if name in red:
            gates[name] = {**(gates.get(name) or base_gates[name]), "baseline": "red"}
    if gates:
        data["gates"] = gates
    if red:
        data["gate_sets"] = _red_sets(sets, base, red)
    elif sets:
        data["gate_sets"] = sets
    return data


def render_pack(d: Detected, *, base: dict, red: frozenset[str] = frozenset()) -> str:
    """The pack's text: top-level blocks dumped one by one, with whole comment lines between them."""
    data = pack_data(d, base=base, red=red)
    blocks = [HEADER + _dump({key: data[key] for key in ("version", "engine", "starter", "gates_confirmed")}),
              f"# {PERMISSION_COMMENT}\n" + _dump({"agents": data["agents"]})]
    if "project" in data:
        blocks.append("# what differs from the engine defaults\n" + _dump({"project": data["project"]}))
    if "work_source" in data:
        blocks.append("# the Backlog: one module per spec file in project.docs.specs; a new feature card writes a new "
                      "spec\n" + _dump({"work_source": data["work_source"], "dashboard": data["dashboard"]}))
    if "gates" in data:
        comment = f"# {_GATES_FROM.get(d.gate_source, 'detected')}; each gate replaces the starter's gate of its name\n"
        if red:
            comment += ("# a gate marked `baseline: red` failed on the base branch when this pack was written: fix it "
                        "and remove `baseline`, or delete the gate\n")
        blocks.append(comment + _dump({"gates": data["gates"]}))
    if "gate_sets" in data:
        blocks.append("# which gates run when; a set not written here is the starter's\n"
                      + _dump({"gate_sets": data["gate_sets"]}))
    return "\n".join(blocks)


# --- git ------------------------------------------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> tuple[int, str]:
    """`(exit code, stdout)` of `git -C repo args`, or `(code, stderr)` when it failed; never raises."""
    env = {**{k: v for k, v in os.environ.items() if k not in GIT_ENV}, "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, errors="replace", timeout=GIT_TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired:
        return 1, f"git {' '.join(args[:2])} timed out"
    except OSError as exc:
        return 1, str(exc)
    return proc.returncode, proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout).strip()


def _commit_of(repo: Path, ref: str | None) -> str | None:
    """The commit `ref` names here, or None when it names none (or could be read as an option)."""
    if not ref or not _REF.fullmatch(ref) or ".." in ref:
        return None
    code, out = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return out.strip() if code == 0 and out.strip() else None


def _footprint(repo: Path) -> tuple[set[str], dict[str, tuple[int, int]]]:
    """The repository's local git config lines and its hooks (name: size, modification time)."""
    code, out = _git(repo, "config", "--local", "--list")
    config = set(out.splitlines()) if code == 0 else set()
    hooks: dict[str, tuple[int, int]] = {}
    code, common = _git(repo, "rev-parse", "--git-common-dir")
    if code == 0 and common.strip():
        folder = Path(repo) / common.strip()
        try:
            with os.scandir(folder / "hooks") as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    hooks[entry.name] = (info.st_size, info.st_mtime_ns)
        except OSError:
            pass
    return config, hooks


def _report_footprint(before: tuple[set[str], dict], after: tuple[set[str], dict], out: TextIO) -> None:
    lines = [*(f"  .git/config + {_show(line)}" for line in sorted(after[0] - before[0])),
             *(f"  .git/config - {_show(line)}" for line in sorted(before[0] - after[0])),
             *(f"  hooks: added {_show(name)}" for name in sorted(set(after[1]) - set(before[1]))),
             *(f"  hooks: removed {_show(name)}" for name in sorted(set(before[1]) - set(after[1]))),
             *(f"  hooks: changed {_show(name)}" for name in sorted(set(after[1]) & set(before[1]))
               if after[1][name] != before[1][name])]
    if lines:
        print("baseline: a gate changed this repository's git setup; check that you want it:", file=out)
        for line in lines:
            print(line, file=out)


def _rmtree(path: Path, out: TextIO) -> None:
    def writable(func, failed, _exc) -> None:  # a read-only folder a gate made: open it, then try again
        os.chmod(os.path.dirname(failed), 0o700)
        func(failed)

    try:
        shutil.rmtree(path, onexc=writable)
    except OSError as exc:
        print(f"warning: init could not delete {path}: {exc}", file=out)


def _remove_worktree(repo: Path, directory: Path, out: TextIO) -> None:
    code, message = _git(repo, "worktree", "remove", "--force", str(directory))
    if os.path.lexists(directory):
        _rmtree(directory, out)
    if code != 0:
        print(f"warning: git did not remove the worktree {directory} ({_show(message)}); its folder is deleted, and "
              "the next `agentflow init --baseline` clears git's record of it", file=out)


def _clean_leftovers(repo: Path, state: Path, out: TextIO) -> None:
    """Remove the worktrees an earlier baseline left under `<state>/baseline-*`, and no other."""
    code, listing = _git(repo, "worktree", "list", "--porcelain", "-z")
    if code != 0:
        print(f"warning: init could not list the worktrees: {_show(listing)}", file=out)
        return
    mine = os.path.realpath(state)
    for field in listing.split("\0"):
        if not field.startswith("worktree "):
            continue
        path = field.removeprefix("worktree ")
        real = os.path.realpath(path)
        if os.path.dirname(real) != mine or not os.path.basename(real).startswith("baseline-"):
            continue
        code, message = _git(repo, "worktree", "remove", "--force", path)
        if code != 0 and not os.path.lexists(path):
            _git(repo, "worktree", "prune")
            print(f"init: git would not remove the leftover worktree {_show(path)}, whose folder is gone, so init ran "
                  "`git worktree prune`, which forgets every worktree whose folder is missing", file=out)
        elif code != 0:
            print(f"warning: git did not remove the leftover worktree {_show(path)}: {_show(message)}", file=out)
        if os.path.lexists(path):
            _rmtree(Path(path), out)


# --- the baseline ---------------------------------------------------------------------------------------------------

class _Interrupts:
    """While inside, SIGTERM and SIGHUP raise `KeyboardInterrupt`; the previous handlers come back on the way out.
    Inside `deferred()`, those signals and SIGINT wait until the block is done, then raise."""

    def __init__(self) -> None:
        self.previous: dict[int, Any] = {}
        self.deferring = False
        self.pending: int | None = None

    def _handler(self, signum: int, _frame: Any) -> None:
        if self.deferring:
            self.pending = signum
            return
        raise KeyboardInterrupt(f"signal {signum}")

    def _install(self, signums: tuple[int, ...]) -> dict[int, Any]:
        installed: dict[int, Any] = {}
        for signum in signums:
            try:
                installed[signum] = signal.signal(signum, self._handler)
            except ValueError:  # not the main thread: signals stay as they are
                break
        return installed

    @staticmethod
    def _restore(handlers: dict[int, Any]) -> None:
        for signum, handler in handlers.items():
            signal.signal(signum, handler if handler is not None else signal.SIG_DFL)

    def __enter__(self) -> _Interrupts:
        self.previous = self._install((signal.SIGTERM, signal.SIGHUP))
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._restore(self.previous)

    @contextmanager
    def deferred(self) -> Iterator[None]:
        self.deferring = True
        sigint = self._install((signal.SIGINT,)) if self.previous else {}
        try:
            yield
        finally:
            self._restore(sigint)
            self.deferring = False
        if self.pending is not None:
            raise KeyboardInterrupt(f"signal {self.pending}")


class BaselineBusy(RuntimeError):
    """Another `agentflow init --baseline` holds this repository's baseline lock."""


def _lock(state: Path) -> int:
    """`<state>/init.lock`, opened and locked (exclusive, without waiting); `BaselineBusy` when another holds it."""
    path = state / "init.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise BaselineBusy(f"another `agentflow init --baseline` is running on this repository (it holds {path}): "
                           "wait for it to finish, then run init again") from None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _gates_of(cfg: Config, set_name: str) -> list[Gate]:
    members = cfg.gate_sets.get(set_name) or []
    try:
        return resolve(cfg, list(members))
    except (KeyError, RecursionError):
        return [cfg.gates[m] for m in members if m in cfg.gates]


def _absent(gate: Gate, missing: set[str]) -> str | None:
    try:
        programs = doctor.gate_programs(gate.run)
    except ValueError:
        return None
    return next((p for p in programs if p in missing), None)


def _row(gate: Gate, result: GateResult) -> BaselineRow:
    seconds = result.duration_s
    if result.skipped:
        return BaselineRow(gate.name, gate.run, "skipped", seconds, result.reason)
    if result.passed:
        return BaselineRow(gate.name, gate.run, "pass", seconds)
    match = _EXIT.fullmatch(result.reason)
    code = int(match.group(1)) if match else None
    said = next((line.strip() for line in reversed(result.tail.splitlines())
                 if any(mark in line for mark in _COULD_NOT_START)), None)
    if code is not None and 0 < code < 128 and code not in (126, 127) and said is None:
        return BaselineRow(gate.name, gate.run, "fail", seconds, result.reason)
    if code in (126, 127):
        note = f"exit {code}: the command could not run (not found, or not executable)"
    elif code is not None and code < 0:
        note = f"killed by signal {-code}"
    elif code is not None and code >= 128:
        note = f"exit {code}: killed by a signal ({code - 128}), not a test result"
    elif said is not None:
        note = f"exit {code}: a command could not start ({said[:120]})"
    else:
        note = result.reason
    return BaselineRow(gate.name, gate.run, "error", seconds, note)


def _not_run(gates: list[Gate], note: str) -> list[BaselineRow]:
    return [BaselineRow(g.name, g.run, "not run", 0.0, note) for g in gates]


def _run_all(cfg: Config, setup: list[Gate], checks: list[Gate], worktree: Path, logs: Path,
             env: dict[str, str], missing: set[str]) -> list[BaselineRow]:
    rows: list[BaselineRow] = []
    for i, gate in enumerate(setup):
        absent = _absent(gate, missing)
        if absent is not None:
            rows.append(BaselineRow(gate.name, gate.run, "not run", 0.0, f"`{absent}` is not on PATH"))
        else:
            (result,) = run_gates(cfg, [gate.name], worktree, logs, env_base=env)
            if result.passed or result.skipped:
                rows.append(_row(gate, result))
                continue
            rows.append(BaselineRow(gate.name, gate.run, "error", result.duration_s, f"setup failed: {result.reason}"))
        return rows + _not_run([*setup[i + 1:], *checks], f"setup `{gate.name}` did not complete")
    for gate in checks:
        absent = _absent(gate, missing)
        if absent is not None:
            rows.append(BaselineRow(gate.name, gate.run, "not run", 0.0, f"`{absent}` is not on PATH"))
            continue
        (result,) = run_gates(cfg, [gate.name], worktree, logs, env_base=env)
        rows.append(_row(gate, result))
    return rows


def _where(gate: Gate) -> str:
    return f"  (in {_show(gate.cwd)})" if gate.cwd else ""


def _short_ref(ref: str) -> str:
    """A full ref as people write it: `refs/heads/main` is `main`, `refs/remotes/origin/main` is `origin/main`."""
    return ref.removeprefix("refs/heads/").removeprefix("refs/remotes/")


def _baseline_env() -> dict[str, str]:
    """This process's environment without the caller's Python environment or git variables: `DROPPED_ENV` and
    `GIT_ENV` go, and so do their `bin` folders on PATH (`$VIRTUAL_ENV/bin`, `$CONDA_PREFIX/bin`, and this
    interpreter's own when it runs in a virtualenv, such as the one `uv run --script` makes)."""
    bins = {os.path.realpath(os.path.join(os.environ[k], "bin")) for k in ("VIRTUAL_ENV", "CONDA_PREFIX")
            if os.environ.get(k)}
    if sys.prefix != sys.base_prefix:
        bins.add(os.path.realpath(os.path.join(sys.prefix, "bin")))
    env = {k: v for k, v in os.environ.items() if k not in DROPPED_ENV and k not in GIT_ENV}
    if "PATH" in env:
        env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep)
                                      if not p or os.path.realpath(p) not in bins)
    return env


def baseline(cfg: Config, repo: Path, base_ref: str, missing: set[str], out: TextIO) -> list[BaselineRow]:
    """Run `cfg`'s setup gates, then each gate of `build` and `verify` once, in a throwaway worktree of `base_ref`.

    `missing` holds the programs doctor did not find: a gate running one is `not run`. The first setup gate that
    fails stops the baseline. Only a plain non-zero exit below 128, with no "could not start" line in its output, is
    `fail`. The worktree is removed whatever happens. `<state>/init.lock` is held throughout: `BaselineBusy` when
    another baseline holds it."""
    repo = Path(repo)
    setup = _gates_of(cfg, "setup")
    checks = list({g.name: g for g in [*_gates_of(cfg, "build"), *_gates_of(cfg, "verify")]}.values())
    commit = _commit_of(repo, base_ref)
    if commit is None:
        print(f"baseline not run: `{_show(_short_ref(base_ref))}` does not exist here", file=out)
        return _not_run([*setup, *checks], f"`{_show(_short_ref(base_ref))}` does not exist here")
    state = safeio.ensure_dir(pack.state_dir(repo))
    lock = _lock(state)  # held for the whole baseline: a second init's cleanup must not take this one's worktree
    try:
        with _Interrupts() as interrupts:
            directory: Path | None = None
            before = _footprint(repo)
            try:
                _clean_leftovers(repo, state, out)
                directory = Path(tempfile.mkdtemp(prefix="baseline-", dir=state))
                # no hook runs at the checkout: only the commands printed below run
                code, message = _git(repo, "-c", f"core.hooksPath={os.devnull}", "worktree", "add", "--detach",
                                     str(directory), commit)
                if code != 0:
                    print(f"baseline not run: git could not create the worktree: {_show(message)}", file=out)
                    return _not_run([*setup, *checks], "the worktree could not be created")
                print(f"baseline: running these commands once in {directory}, a worktree of "
                      f"`{_show(_short_ref(base_ref))}` ({commit[:12]}):", file=out)
                for gate in setup:
                    print(f"  setup {gate.name}: {_show(gate.run)}{_where(gate)}", file=out)
                for gate in checks:
                    print(f"  gate  {gate.name}: {_show(gate.run)}{_where(gate)}", file=out)
                env = _baseline_env()
                return _run_all(cfg, setup, checks, directory, state / "baseline-logs", env, missing)
            finally:
                with interrupts.deferred():
                    if directory is not None:
                        _remove_worktree(repo, directory, out)
                    _report_footprint(before, _footprint(repo), out)
    finally:
        os.close(lock)


# --- init -----------------------------------------------------------------------------------------------------------

def _rule_words(words: list[str]) -> list[str] | None:
    """The words of a narrow allow rule for one simple command (`words[0]` is its program), or None when no rule
    narrower than the program itself can be told: an interpreter without `-m <module>` (or `node --test`), a
    subcommand program with no subcommand after its flags, or a word that isn't plain."""
    program, rest = words[0], words[1:]
    if program in _SHELLS or not _PROGRAM.fullmatch(program):
        return None
    if _INTERPRETER.fullmatch(program):
        if program.startswith(("python", "pypy")) and rest[:1] == ["-m"] and len(rest) > 1 and _MODULE.fullmatch(rest[1]):
            return [program, "-m", rest[1]]
        if program in ("node", "nodejs") and rest[:1] == ["--test"]:
            return [program, "--test"]
        return None
    taken, i = [program], 0
    while i < len(rest) and rest[i].startswith("-"):
        if not _FLAG.fullmatch(rest[i]):
            return None
        taken.append(rest[i])
        if rest[i] in _FLAG_ARGS.get(program, ()):  # a flag known to take the next word as its value
            if i + 1 >= len(rest) or not _FLAG.fullmatch("-" + rest[i + 1]):
                return None
            taken.append(rest[i + 1])
            i += 1
        i += 1
    subcommands: list[str] = []
    if program in _DISPATCHERS or len(taken) == 1:  # after a runner's flags come its arguments, not subcommands
        for word in rest[i:i + 2]:
            if not _SUBCOMMAND.fullmatch(word):
                break
            subcommands.append(word)
    if program in _DISPATCHERS and not subcommands:
        return None
    return taken + subcommands


def _allow_rule(run: str) -> str | None:
    """A narrow `Bash(...)` rule for the first program `run` runs, or None when none can be told (`_rule_words`)."""
    try:
        programs = doctor.gate_programs(run)
        commands = doctor.simple_commands(run)
    except ValueError:
        return None
    if not programs:
        return None
    words = next(c for c in commands if c[0] == programs[0])
    taken = _rule_words(words)
    return f"Bash({' '.join(taken)} *)" if taken else None


def _table(rows: list[list[str]]) -> list[str]:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return [("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))).rstrip() for row in rows]


def _short(text: str) -> str:
    return text if len(text) <= _COMMAND_WIDTH else text[: _COMMAND_WIDTH - 1] + "…"


def _print_detection(d: Detected, notes: list[str], out: TextIO) -> None:
    where = f"`{_show(_short_ref(d.base_ref))}`" if d.base_ref else "the working tree (no commits yet)"
    print(f"detected, read from {where}:", file=out)
    print(f"  base branch  {_show(d.base_branch)}", file=out)
    print(f"  starter      {d.starter}", file=out)
    print(f"  gates        {_GATES_FROM.get(d.gate_source, d.gate_source)}", file=out)
    for name, gate in d.gates.items():
        cwd = f"  (in {_show(gate['cwd'])})" if gate.get("cwd") else ""
        print(f"    {_show(name)}: {_show(gate.get('run', ''))}{cwd}", file=out)
    for name, members in d.gate_sets.items():
        print(f"  gate_sets.{name}  {', '.join(_show(m) for m in members) or '-'}", file=out)
    for key, value in d.docs.items():
        print(f"  docs.{key}  {_show(value)}", file=out)
    if d.skills:
        print(f"  skills       {', '.join(_show(s) for s in d.skills)}", file=out)
    if d.agent_docs:
        print(f"  agent docs   {', '.join(d.agent_docs)}", file=out)
    if notes:
        print("notes:", file=out)
        for note in notes:
            print(f"  - {_show(note)}", file=out)


def _print_baseline(rows: list[BaselineRow], out: TextIO) -> None:
    print("baseline:", file=out)
    table = [["GATE", "COMMAND", "STATUS", "SECONDS", "NOTE"]]
    table += [[_show(r.gate), _short(_show(r.command)), r.status, f"{r.seconds:.1f}", _show(r.note)] for r in rows]
    for line in _table(table):
        print(f"  {line}", file=out)


def _effective(d: Detected, data: dict) -> dict:
    return layers.merge([*layers.base_layers(d.starter), layers.Layer("project", data, None)])[0]


def _print_bug_rule(merged: dict, out: TextIO) -> None:
    lanes = merged.get("lanes") if isinstance(merged.get("lanes"), list) else []
    if "bug" not in lanes:
        return
    verify = (merged.get("gate_sets") or {}).get("verify") or []
    gate = (merged.get("gates") or {}).get(verify[0]) if verify else None
    rule = _allow_rule(gate.get("run", "")) if isinstance(gate, dict) else None
    print("bug lane: its red/green check runs the regression test only when a Bash allow rule in "
          ".claude/settings.json covers it.", file=out)
    if rule:
        print(f"  add to `permissions.allow`: \"{_show(rule)}\"", file=out)
    else:
        print("  add a narrow `Bash(<test command> *)` rule for your test command to `permissions.allow`", file=out)


def init(repo: Path, *, starter: str | None, run_baseline: bool, out: TextIO) -> int:
    """Detect, check the machine, run the baseline when asked, and write `.agentflow/config.yaml` once.

    Exits 0 when the pack was written, whatever doctor or the baseline found; 1 (with a message naming `init`) when
    it refuses or fails; 130 when the baseline was interrupted, which writes no pack."""
    repo = Path(repo)
    folder = repo / pack.PACK_DIR
    target = folder / "config.yaml"

    def refuse(message: str) -> int:
        print(f"error: init: {message}", file=out)
        return 1

    if os.path.islink(folder):
        return refuse(SYMLINKED)
    if os.path.lexists(target):
        return refuse(ALREADY)
    if os.path.lexists(folder) and not os.path.isdir(folder):
        return refuse(f"{pack.PACK_DIR} is not a folder")
    if starter is not None and starter not in layers.starter_names():
        return refuse(f"unknown starter {starter!r}; the starters are {', '.join(layers.starter_names())}")

    found = detect.detect(repo, starter)
    base = base_of(found.starter)
    notes = [*found.notes, *_checked(found)[3]]
    provisional = pack.state_dir(repo) / "init" / "config.yaml"
    rows: list[BaselineRow] | None = None
    why_not = None
    try:
        safeio.write_text(provisional, render_pack(found, base=base))
        try:
            cfg, extra = doctor.load_checked(provisional)
        except ConfigError as exc:
            return refuse(f"the pack it rendered does not load ({exc}); no pack was written")
        checks = [*extra, *doctor.run_checks(cfg, repo)]
        if run_baseline:
            if found.base_ref is None:
                why_not = "the repository has no commits yet"
            elif _commit_of(repo, found.base_ref) is None:
                why_not = f"`{_show(_short_ref(found.base_ref))}` does not exist here"
            else:
                try:
                    rows = baseline(cfg, repo, found.base_ref, doctor.missing_programs(cfg, checks), out)
                except BaselineBusy as exc:
                    return refuse(f"{exc}; no pack was written")
        red = frozenset(r.gate for r in rows or [] if r.status == "fail")
        data = pack_data(found, base=base, red=red)
        try:
            if os.path.islink(folder):
                return refuse(SYMLINKED)
            safeio.write_new(target, render_pack(found, base=base, red=red))
        except FileExistsError:
            return refuse(ALREADY)
        except safeio.SymlinkRefused:
            return refuse(SYMLINKED)
    except KeyboardInterrupt:
        print("error: init: interrupted; no pack was written", file=out)
        return 130
    finally:
        try:
            provisional.unlink(missing_ok=True)
            provisional.parent.rmdir()
        except OSError:
            pass

    print(f"\nwrote {target}\n", file=out)
    _print_detection(found, notes, out)
    print("\ndoctor:", file=out)
    print(doctor.render(checks), file=out)
    print("", file=out)
    base_name = _show(found.base_branch)
    if rows is None:
        print(f"baseline not run: {why_not}" if why_not else
              f"baseline not run: pass --baseline to run the detected gates once on `{base_name}`", file=out)
    else:
        _print_baseline(rows, out)
        if any(r.status in ("fail", "error") for r in rows):
            print(f"  logs: {pack.state_dir(repo) / 'baseline-logs'}", file=out)
        if red and set(data["gate_sets"]["verify"]) <= red:
            print(f"every verify gate fails on `{base_name}`: runs cannot pass verify until you fix them", file=out)
    merged = _effective(found, data)
    print("", file=out)
    _print_bug_rule(merged, out)
    print("\nnext steps:", file=out)
    print(f"  1. review `gates` and `gate_sets` in {pack.PACK_DIR}/config.yaml, and the notes above", file=out)
    print("  2. set `gates_confirmed: true` in it", file=out)
    print("  3. agentflow validate", file=out)
    print("  4. agentflow explain feature --module <id>", file=out)
    print("  5. agentflow run feature --module <id> --dry-run", file=out)
    return 0
