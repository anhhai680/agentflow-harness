"""`agentflow doctor`: whether this machine can run the pack, checked before a run spends money.

It looks at the agent CLIs and their logins, `gh`, the `origin` remote, Docker (when a gate runs it), the state dir
and the programs each gate runs. Every external command is bounded by `TIMEOUT_S`. It changes nothing except
creating the state dir and a probe file there that it removes at once.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agentflow import layers, pack, run_env
from agentflow.config import Config, ConfigError, load_config

TIMEOUT_S = 20  # each external command

LOGIN_CHECKS = {"claude": ("auth", "status", "run `claude auth login`"),
                "codex": ("login", "status", "run `codex login`")}
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_OPERATOR_CHARS = set("();|&\n")
_BUILTINS = frozenset("true false test [ [[ cd export set source . exec command env time if then elif else fi for do "
                      "done while until case esac { } ! exit trap".split())
_PREFIXES = frozenset("if then elif else do while until { ! time".split())  # a keyword the command follows on


@dataclass(frozen=True)
class Check:
    name: str
    status: Literal["ok", "warn", "fail"]
    detail: str
    fix: str = ""


def simple_commands(command: str) -> list[list[str]]:
    """The words of each simple command in a shell `command`, in order, once `NAME=value` prefixes and a leading
    keyword (`if`, `then`, `do`, `!` ...) are skipped; an empty command is left out. Raises `ValueError` when the
    command can't be tokenised (an unbalanced quote)."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";()<>|&\n")
    lexer.commenters = "#"
    lexer.whitespace = " \t\r"  # a newline separates commands, like `;`
    lexer.whitespace_split = True
    segments: list[list[str]] = [[]]
    for token in lexer:
        if token and set(token) <= _OPERATOR_CHARS:
            segments.append([])
        else:
            segments[-1].append(token)
    commands: list[list[str]] = []
    for words in segments:
        rest = list(words)
        while rest and (_ASSIGNMENT.match(rest[0]) or rest[0] in _PREFIXES):
            rest.pop(0)
        if rest:
            commands.append(rest)
    return commands


def gate_programs(command: str) -> list[str]:
    """The programs a gate's shell `command` runs, in order and without repeats: the first word of each simple
    command (`simple_commands`). A shell builtin or keyword, and a word starting with `$`, is no program. Raises
    `ValueError` when the command can't be tokenised (an unbalanced quote)."""
    programs: list[str] = []
    for words in simple_commands(command):
        if words[0] in _BUILTINS or words[0].startswith("$") or words[0] in programs:
            continue
        programs.append(words[0])
    return programs


def _run(argv: list[str], cwd: Path) -> tuple[int | None, str]:
    """`(exit code, output)` of `argv`; the code is None when it timed out or could not start."""
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True)
    except OSError as exc:
        return None, str(exc)
    try:
        output, _ = proc.communicate(timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.communicate()
        return None, "timed out"
    return proc.returncode, output.strip()


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _failed(name: str, code: int | None, output: str, fix: str) -> Check:
    detail = "timed out" if output == "timed out" and code is None else (_last_line(output) or f"exit {code}")
    return Check(name, "fail", detail, fix)


def _agent_checks(cfg: Config, repo: Path) -> list[Check]:
    checks: list[Check] = []
    for name, spec in cfg.agents.items():
        if not spec.enabled:
            continue
        label = f"agent:{name}"
        program = spec.command[0] if spec.command else ""
        install = f"install `{program}`, or set `agents.{name}.enabled: false`"
        if not program or "/" in program or os.sep in program:
            checks.append(Check(label, "fail", f"{program or 'no command'!r} is not a bare command name",
                                f"set `agents.{name}.command` to a program on PATH, or set `agents.{name}.enabled: false`"))
            continue
        found = shutil.which(program)
        if not found:
            checks.append(Check(label, "fail", f"`{program}` is not on PATH", install))
            continue
        login = LOGIN_CHECKS.get(program)
        if login is None:
            checks.append(Check(label, "warn", f"{found} found; login not checked"))
            continue
        code, output = _run([*spec.command, login[0], login[1]], repo)
        if code == 0:
            checks.append(Check(label, "ok", f"{found}, logged in"))
        else:
            checks.append(_failed(label, code, output, login[2]))
    return checks


def _gh_check(repo: Path) -> Check:
    if not shutil.which("gh"):
        return Check("gh", "fail", "`gh` is not on PATH", "install gh (https://cli.github.com), then run `gh auth login`")
    code, output = _run(["gh", "auth", "status"], repo)
    if code == 0:
        return Check("gh", "ok", "signed in (this check needs the network)")
    check = _failed("gh", code, output, "run `gh auth login`")
    return Check("gh", "fail", f"{check.detail} (this check needs the network)", check.fix)


def _remote_check(repo: Path) -> Check:
    code, output = _run(["git", "remote", "get-url", "origin"], repo)
    if code == 0 and output:
        return Check("remote", "ok", f"origin is {output}")
    return Check("remote", "fail", _last_line(output) or "no remote named origin",
                 "add a remote named origin: `finish` pushes the run's branch there")


def _docker_check(repo: Path) -> Check:
    if not shutil.which("docker"):
        return Check("docker", "fail", "`docker` is not on PATH", "start Docker (install it first)")
    code, output = _run(["docker", "info"], repo)
    if code == 0:
        return Check("docker", "ok", "the daemon answers")
    return _failed("docker", code, output, "start Docker")


def _state_dir_check(repo: Path) -> Check:
    try:
        directory = pack.state_dir(repo)
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".doctor-{os.getpid()}"
        probe.write_text("probe\n", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check("state dir", "fail", str(exc), "make `paths.state_dir` (or its default) a directory you can write to")
    return Check("state dir", "ok", f"{directory} is writable")


MAX_LISTED_NAMES = 12


def _environment_check(cfg: Config) -> Check:
    """The inherited variables a run does not pass on, by name and reason; never a value."""
    rows = run_env.dropped(cfg)
    if not rows:
        return Check("inherited environment", "ok", "nothing is dropped from the environment a run inherits")
    by_reason: dict[str, list[str]] = {}
    for name, reason in rows:
        by_reason.setdefault(reason, []).append(name[:64])
    remaining = MAX_LISTED_NAMES
    parts: list[str] = []
    for reason, names in by_reason.items():
        shown = names[:remaining]
        remaining -= len(shown)
        extra = len(names) - len(shown)
        text = ", ".join(shown) + (f"{' and ' if shown else ''}{extra} more" if extra else "")
        parts.append(f"{text} ({reason})")
    return Check("inherited environment", "warn", f"{len(rows)} dropped: " + "; ".join(parts),
                 "they are dropped from runs; to keep one, set it in `defaults.env`")


def gate_path(cfg: Config) -> str:
    """The PATH a gate's programs are looked up on: `defaults.env.PATH`, else this process's."""
    return cfg.defaults.env.get("PATH") or os.environ.get("PATH", os.defpath)


def found(program: str, path: str) -> bool:
    """True when `program` is on `path`: a bare name found there, or an absolute path that exists. A relative path
    (`./gradlew`) is never found: it may be created by setup."""
    return bool(shutil.which(program, path=path)) and (os.path.isabs(program) or "/" not in program)


def missing_programs(cfg: Config, checks: list[Check]) -> set[str]:
    """The programs behind the failed `gate:<name>` checks in `checks` that are not on the gates' PATH, and `docker`
    when the `docker` check failed (its daemon doesn't answer, so a gate running it can't run)."""
    path = gate_path(cfg)
    missing: set[str] = {"docker"} if any(c.name == "docker" and c.status == "fail" for c in checks) else set()
    for check in checks:
        name = check.name.removeprefix("gate:")
        if check.status != "fail" or name == check.name or name not in cfg.gates:
            continue
        try:
            programs = gate_programs(cfg.gates[name].run)
        except ValueError:
            continue
        missing.update(p for p in programs if not found(p, path))
    return missing


def load_checked(path: Path) -> tuple[Config, list[Check]]:
    """The config at `path`, layered. A broken user file is a failed `user config` check, and the config is loaded
    without it; any other `ConfigError` is raised."""
    try:
        return load_config(path), []
    except ConfigError as exc:
        user_file = str(layers.user_config_path())
        if user_file not in str(exc):
            raise
        check = Check("user config", "fail", str(exc), f"fix or delete {user_file} (`agentflow validate` shows the same error)")
        return load_config(path, user=False), [check]


def _gate_names(cfg: Config) -> list[str]:
    names: list[str] = []
    for members in cfg.gate_sets.values():
        for member in members:
            if member in cfg.gates and member not in names:
                names.append(member)
    return names


def _gate_checks(cfg: Config) -> tuple[list[Check], bool]:
    """The `gate:<name>` checks, and whether any gate runs `docker`."""
    path = gate_path(cfg)
    has_setup = bool(cfg.gate_sets.get("setup"))
    checks: list[Check] = []
    uses_docker = False
    for name in _gate_names(cfg):
        label = f"gate:{name}"
        try:
            programs = gate_programs(cfg.gates[name].run)
        except ValueError as exc:
            checks.append(Check(label, "warn", f"cannot read the command: {exc}"))
            continue
        uses_docker = uses_docker or "docker" in programs
        problems: list[Check] = []
        for program in programs:
            if found(program, path):
                continue
            if "/" in program and not os.path.isabs(program):
                problems.append(Check(label, "warn", f"`{program}` is relative: it may be created by setup"))
            elif has_setup:
                problems.append(Check(label, "warn", f"`{program}` is not on PATH: it may be installed by `gate_sets.setup`"))
            else:
                problems.append(Check(label, "fail", f"`{program}` is not on PATH", f"install `{program}`"))
        if not problems:
            checks.append(Check(label, "ok", ", ".join(f"`{p}`" for p in programs) + " found" if programs else "no program to find"))
        else:
            worst = next((p for p in problems if p.status == "fail"), problems[0])
            checks.append(Check(label, worst.status, "; ".join(p.detail for p in problems), worst.fix))
    return checks, uses_docker


def run_checks(cfg: Config, repo: Path) -> list[Check]:
    """Every check, in order: the agents, `gh`, the remote, Docker (when a gate runs it), the state dir, the gates."""
    gate_checks, uses_docker = _gate_checks(cfg)
    checks = [*_agent_checks(cfg, repo), _gh_check(repo), _remote_check(repo)]
    if uses_docker:
        checks.append(_docker_check(repo))
    checks.append(_state_dir_check(repo))
    checks.append(_environment_check(cfg))
    return [*checks, *gate_checks]


def printable(text: object) -> str:
    """`text` safe to print on a terminal: a newline or a control character (an escape sequence a repository or a
    tool's output may hold) is written as its backslash escape."""
    return "".join(ch if ch.isprintable() else ch.encode("unicode_escape").decode("ascii") for ch in str(text))


def render(checks: list[Check]) -> str:
    """One line per check (and an indented fix under a warning or failure), then a summary line. Every text is
    `printable`: a check's detail can quote the repository or a tool's output."""
    names = [printable(c.name) for c in checks]
    width = max((len(n) for n in names), default=0)
    lines: list[str] = []
    for check, name in zip(checks, names):
        lines.append(f"{check.status:<4}  {name:<{width}}  {printable(check.detail)}")
        if check.fix and check.status != "ok":
            lines.append(f"      {'':<{width}}  fix: {printable(check.fix)}")
    failed = sum(c.status == "fail" for c in checks)
    warned = sum(c.status == "warn" for c in checks)
    lines.append(f"{len(checks)} checks: {len(checks) - failed - warned} ok, {warned} warning{'s' if warned != 1 else ''}, "
                 f"{failed} failed")
    return "\n".join(lines)
