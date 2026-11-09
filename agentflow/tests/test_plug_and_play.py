"""Plug and play, end to end: `agentflow init` in a repository that has never seen agentflow, then `doctor`,
`validate`, `explain` and a dry run, with nothing written to the repository but `.agentflow/config.yaml`.

Each toy repository under `fixtures/repos/` is copied into a tmp git repo on `main`, pushed to a bare `origin` (with
no `remote set-head`, so the base branch is found the way a fresh clone's would be). A tmp `bin/` first on PATH holds
a logged-in `claude` (the scripted fake agent otherwise), the tests' `gh`, and `pnpm` and `uv` fakes that exit 0, so
no real package manager, model or network is used.

The lessons loop runs end to end the same way, through the CLI: feature runs, `agentflow retro`, a merged lessons
PR, the runs that carry the lesson, `agentflow lessons` and a second retro.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agentflow import backlog, pack
from agentflow.config import load_config
from conftest import ENGINE, ROOT, run_git

REPOS = ENGINE / "tests" / "fixtures" / "repos"
FLOW = ENGINE / "flow.py"
WHITESPACE = 'git -c core.whitespace=-blank-at-eol diff --check "$(git hash-object -t tree /dev/null)"'
EXPECTED = {  # the verify gates' commands each repository's pack ends up with, and where they came from
    "python": (["python3 -m unittest -q"], "the CI workflow"),
    "node": (["pnpm run test"], "package.json scripts"),
    "docs": ([WHITESPACE], "the docs-only starter"),
}
CONFIRM_WARNING = "warning gates_confirmed:"
PRINTED_RULE = re.compile(r'^  add to `permissions\.allow`: "(Bash\(.+\))"$', re.MULTILINE)


def _script(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fakebin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bins = tmp_path / "bin"
    bins.mkdir()
    agent = f"{shlex.quote(sys.executable)} {shlex.quote(str(ENGINE / 'tests' / 'fake_agent.py'))}"
    _script(bins / "claude", f'if [ "$1 $2" = "auth status" ]; then exit 0; fi\nexec {agent} "$@"\n')
    shutil.copy2(ENGINE / "tests" / "bin" / "gh", bins / "gh")
    for name in ("pnpm", "uv"):
        _script(bins / name, "exit 0\n")
    monkeypatch.setenv("PATH", f"{bins}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("FAKE_GH_FAIL", raising=False)
    return bins


def _new_repo(root: Path) -> None:
    run_git(root, "init", "-q", "-b", "main")
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        run_git(root, "config", key, value)


def toy(kind: str, tmp_path: Path) -> Path:
    """`fixtures/repos/<kind>` as a git repo on `main` with one commit, pushed to a bare `origin`."""
    origin = tmp_path / "origin.git"
    run_git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    repo = tmp_path / kind
    shutil.copytree(REPOS / kind, repo, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))
    _new_repo(repo)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "initial")
    run_git(repo, "remote", "add", "origin", str(origin))
    run_git(repo, "push", "-q", "origin", "main")
    return repo.resolve()


def agentflow(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(FLOW), *args], cwd=repo, capture_output=True, text=True, timeout=300)


def git_status(repo: Path) -> str:
    return run_git(repo, "status", "--porcelain", "--untracked-files=all").strip()


def confirm_gates(repo: Path) -> None:
    """What the human does after reading init's gate table: flip `gates_confirmed` in the pack."""
    path = repo / ".agentflow" / "config.yaml"
    text = path.read_text(encoding="utf-8")
    assert text.count("gates_confirmed: false") == 1, text
    path.write_text(text.replace("gates_confirmed: false", "gates_confirmed: true"), encoding="utf-8")


def add_bug_lane_rule(repo: Path, init_out: str) -> str:
    """What the human does for the bug lane (playbook §13): add the `Bash(...)` rule init printed to their own
    `.claude/settings.local.json`, which git ignores (Claude Code keeps it out of git; here `.git/info/exclude`)."""
    found = PRINTED_RULE.findall(init_out)
    assert len(found) == 1, init_out
    settings = repo / ".claude" / "settings.local.json"
    assert not settings.exists()
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"permissions": {"allow": found}}), encoding="utf-8")
    exclude = Path(run_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").strip())
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write("/.claude/settings.local.json\n")
    return found[0]


def verify_commands(repo: Path) -> list[str]:
    cfg = load_config(pack.pack_config(repo))
    return [cfg.gates[name].run for name in cfg.gate_sets["verify"]]


def _shown(proc: subprocess.CompletedProcess) -> str:
    return f"exit {proc.returncode}\n{proc.stdout}{proc.stderr}"


@pytest.mark.parametrize("kind", sorted(EXPECTED))
def test_init_to_dry_run(kind: str, tmp_path: Path, fakebin: Path) -> None:
    repo = toy(kind, tmp_path)
    if kind == "docs":
        assert not (repo / ".claude").exists()  # a repository with no skills of its own runs on the bundled ones

    init = agentflow(repo, "init")
    assert init.returncode == 0, _shown(init)
    commands, source = EXPECTED[kind]
    assert verify_commands(repo) == commands, f"verify gates should come from {source}\n{init.stdout}"

    doctor = agentflow(repo, "doctor")
    assert doctor.returncode == 0, _shown(doctor)

    add_bug_lane_rule(repo, init.stdout)
    confirm_gates(repo)
    validate = agentflow(repo, "validate")
    assert validate.returncode == 0 and "(0 warnings)" in validate.stdout, _shown(validate)

    explain = agentflow(repo, "explain", "feature", "--module", "greet")
    assert explain.returncode == 0, _shown(explain)
    dry_run = agentflow(repo, "run", "feature", "--module", "greet", "--dry-run")
    assert dry_run.returncode == 0, _shown(dry_run)
    assert all(command in dry_run.stdout for command in commands), _shown(dry_run)

    assert git_status(repo) == "?? .agentflow/config.yaml"
    shutil.rmtree(repo / ".agentflow")
    assert git_status(repo) == ""


def test_init_in_a_repository_with_specs_writes_a_specs_backlog(tmp_path: Path, fakebin: Path) -> None:
    repo = toy("docs", tmp_path)
    proposals = repo / "docs" / "proposals"
    proposals.mkdir()
    for name in ("greet.md", "index.md", "README.md"):
        (proposals / name).write_text("# Greet\n\nStatus: approved\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "proposals")
    run_git(repo, "push", "-q", "origin", "main")

    init = agentflow(repo, "init")
    assert init.returncode == 0, _shown(init)
    cfg = load_config(pack.pack_config(repo))
    assert cfg.work_source.kind == "specs" and cfg.project.docs["specs"] == "docs/proposals"

    add_bug_lane_rule(repo, init.stdout)
    validate = agentflow(repo, "validate")
    assert validate.returncode == 0, _shown(validate)
    warnings = [line for line in validate.stdout.splitlines() if line.startswith("warning ")]
    assert len(warnings) == 1 and warnings[0].startswith(CONFIRM_WARNING), _shown(validate)
    confirm_gates(repo)

    cards = backlog.snapshot(load_config(pack.pack_config(repo)), repo)["cards"]
    assert sorted(c["id"] for c in cards if c["kind"] == "module") == ["greet"], cards
    dry_run = agentflow(repo, "run", "feature", "--module", "greet", "--dry-run")
    assert dry_run.returncode == 0, _shown(dry_run)
    assert git_status(repo) == "?? .agentflow/config.yaml"


def test_init_then_validate_for_a_starter_in_an_empty_repo(tmp_path: Path, fakebin: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    _new_repo(repo)

    init = agentflow(repo, "init", "--starter", "python-uv")
    assert init.returncode == 0, _shown(init)
    add_bug_lane_rule(repo, init.stdout)
    validate = agentflow(repo, "validate")
    assert validate.returncode == 0, _shown(validate)
    warnings = [line for line in validate.stdout.splitlines() if line.startswith("warning ")]
    assert len(warnings) == 1 and warnings[0].startswith(CONFIRM_WARNING), _shown(validate)
    assert "(1 warnings)" in validate.stdout

    confirm_gates(repo)
    validate = agentflow(repo, "validate")
    assert validate.returncode == 0 and "(0 warnings)" in validate.stdout, _shown(validate)
    assert git_status(repo) == "?? .agentflow/config.yaml"


def test_security_overlay_covers_the_new_modules() -> None:
    """The modules that parse an untrusted repository, write its pack or merge the config layers are reviewed with
    the security overlay, like the guards they feed."""
    when_paths = load_config(pack.pack_config(ROOT)).overlays["security"]["when_paths"]
    for module in ("agentflow/layers.py", "agentflow/detect.py", "agentflow/init.py"):
        assert module in when_paths


# --------------------------------------------------------------------------- the lessons loop, end to end

LESSONS_PACK = """\
version: 2
engine: ">=2,<3"
gates:
  unit: {run: "test -f fixed.txt"}
gate_sets: {setup: [], build: [unit], verify: [unit]}
agents: {claude: {permission_mode: bypassPermissions}}
lanes: [feature]
phases:
  build:
    needs_approval: false
    steps:
      - {id: build, lanes: [feature], skill: agentflow-build, role: build, prompt: step.md, gates: build,
         fix_loop: true, commit: true, message: "Build {module}"}
"""
BUILD = "build/build"
RETRO = "retro/retro"
DONE = {"status": "done", "summary": "did it"}
FIXED = {"write": {"fixed.txt": "fixed\n"}, "result": DONE}
LESSON_TITLE = "Write fixed.txt before finishing a build step"
LESSON_BODY = "The unit gate fails on the first pass until fixed.txt exists; write it first."
RETRO_PR = "https://github.com/acme/toy/pull/9"


def _lessons_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The `docs` toy with a pack whose one step must write `fixed.txt` for gate `unit` to pass, the pack committed
    on `main` and pushed, and the tests' `gh` logging to `<tmp>/gh.jsonl`."""
    repo = toy("docs", tmp_path)
    (repo / ".agentflow").mkdir()
    (repo / ".agentflow" / "config.yaml").write_text(LESSONS_PACK, encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "Add the pack")
    run_git(repo, "push", "-q", "origin", "main")
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.jsonl"))
    monkeypatch.setenv("FAKE_GH_URL", RETRO_PR)
    return repo


def _feature_run(repo: Path, fake, module: str, acts: list[dict]) -> tuple[str, list[dict], list[dict]]:
    """`agentflow run feature --module <module>` with the build step's acts: the run id, its events and the agent
    calls it made."""
    from agentflow.state import runs_root

    before = len(fake.calls())
    fake.script({BUILD: acts})
    proc = agentflow(repo, "run", "feature", "--module", module)
    assert proc.returncode == 0, _shown(proc)
    (run_id,) = [p.name for p in runs_root(repo).iterdir() if p.name.endswith(f"-feature-{module}")]
    events = [json.loads(line) for line in (runs_root(repo) / run_id / "events.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert [e for e in events if e["type"] == "run_finished"], _shown(proc)
    return run_id, events, fake.calls()[before:]


def _first_unit(events: list[dict]) -> dict:
    return next(e for e in events if e["type"] == "gate_result" and e.get("gate") == "unit")


def _retro(repo: Path, fake, result: dict) -> tuple[str, str]:
    """`agentflow retro` with the session's result: its pull request body and the branch it pushed."""
    from agentflow.state import Run, runs_root

    fake.script({RETRO: [{"result": result}]})
    before = {p.name for p in runs_root(repo).iterdir()}
    proc = agentflow(repo, "retro")
    assert proc.returncode == 0 and f"Pull request: {RETRO_PR}" in proc.stdout, _shown(proc)
    (run_id,) = [p.name for p in runs_root(repo).iterdir() if p.name not in before and "-retro-" in p.name]
    run = Run.load(repo, run_id)
    assert run.state["status"] == "done" and run.state["pr_url"] == RETRO_PR
    return (run.dir / "retro" / "pr.md").read_text(encoding="utf-8"), run.state["branch"]


def _proposal(add=(), renew=()) -> dict:
    return {"status": "done", "summary": "the runs taught one thing",
            "lessons": {"add": list(add), "expire": [], "renew": list(renew)}, "config_notes": [], "engine_issues": []}


def _lesson_of(repo: Path, lesson_id: str) -> dict:
    proc = agentflow(repo, "lessons", "--json")
    assert proc.returncode == 0, _shown(proc)
    (row,) = [r for r in json.loads(proc.stdout)["lessons"] if r["id"] == lesson_id]
    return row


def test_lessons_loop_end_to_end(tmp_path: Path, fakebin: Path, fake, monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs fail a gate, a retro proposes a lesson as a PR, a human merges it, later runs carry it and pass, the
    lesson's effect reads `improved`, the next retro reports that effect first, and a lesson past its `review_by`
    leaves the prompt."""
    from datetime import date, timedelta

    from agentflow import pack as pack_mod
    from agentflow.lessons import LESSONS_HEADING

    repo = _lessons_repo(tmp_path, monkeypatch)
    gh_log = tmp_path / "gh.jsonl"

    # 1. Two runs whose step forgets `fixed.txt`: gate `unit` fails its first pass and the fix loop mends it.
    sources = []
    for module in ("one", "two"):
        run_id, events, _calls = _feature_run(repo, fake, module, [{"result": DONE}, FIXED])
        first = _first_unit(events)
        assert first["passed"] is False, events
        assert [e for e in events if e["type"] == "fix_attempt"]
        sources.append(f"{run_id}#{first['seq']}")

    # 2. The retro opens a draft PR that changes only the lessons file.
    add = {"title": LESSON_TITLE, "body": LESSON_BODY, "applies": ["build"], "target": "gate_first_pass:unit",
           "sources": sources, "engine_candidate": False}
    body, branch = _retro(repo, fake, _proposal(add=[add]))
    created = [json.loads(line) for line in gh_log.read_text(encoding="utf-8").splitlines()]
    assert [c[:2] for c in created if c[:2] == ["pr", "create"]] == [["pr", "create"]]
    run_git(repo, "fetch", "-q", "origin")
    assert run_git(repo, "diff", "--name-only", "main", f"origin/{branch}").split() == [pack_mod.LESSONS_FILE]
    assert "## L-0001 " + LESSON_TITLE in run_git(repo, "show", f"origin/{branch}:{pack_mod.LESSONS_FILE}")

    # 3. A human merges it.
    run_git(repo, "merge", "-q", "--no-edit", f"origin/{branch}")
    run_git(repo, "push", "-q", "origin", "main")

    # 4. The next run's first prompt carries the lesson under "Lessons from earlier runs".
    after = []
    run_id, events, calls = _feature_run(repo, fake, "three", [FIXED])
    prompt = calls[0]["argv"][-1]
    assert LESSONS_HEADING in prompt
    assert f"- **L-0001** {LESSON_TITLE}: {LESSON_BODY}" in prompt.split(LESSONS_HEADING, 1)[1].split("\n## ", 1)[0]
    assert any("L-0001" in e["ids"] for e in events if e["type"] == "lessons_injected")
    after.append(events)

    # 5. Three more runs carry it and pass `unit` on the first pass: the lesson's effect reads `improved`.
    for module in ("four", "five", "six"):
        after.append(_feature_run(repo, fake, module, [FIXED])[1])
    assert all(_first_unit(events)["passed"] for events in after)
    row = _lesson_of(repo, "L-0001")
    assert (row["verdict"], row["baseline"], row["baseline_runs"], row["after"], row["after_runs"]) == \
        ("improved", 0.0, 2, 1.0, 4), row
    assert row["target"] == "gate_first_pass:unit" and row["recorded_baseline"].startswith("0.00 over 2 runs")

    # 6. The second retro's PR opens with the effect of the lessons before it, verdict and all.
    renew = {"id": "L-0001", "review_by": (date.today() + timedelta(days=60)).isoformat(), "reason": "it works"}
    body, _branch = _retro(repo, fake, _proposal(renew=[renew]))
    headings = [line for line in body.splitlines() if line.startswith("## ")]
    assert headings[0] == "## Effect of previous lessons", body
    effect = body.split("## Effect of previous lessons\n", 1)[1].split("\n## ", 1)[0]
    (line,) = [x for x in effect.splitlines() if x.startswith("L-0001")]
    assert "improved" in line and "gate_first_pass:unit" in line, effect

    # 7. With `review_by` in the past the lesson leaves the next prompt.
    lessons_file = repo / pack_mod.LESSONS_FILE
    text = lessons_file.read_text(encoding="utf-8")
    lessons_file.write_text(re.sub(r"- review_by: \S+", "- review_by: 2020-01-01", text), encoding="utf-8")
    run_git(repo, "commit", "-q", "-am", "Expire L-0001")
    _run_id, events, calls = _feature_run(repo, fake, "seven", [FIXED])
    assert "**L-0001**" not in calls[0]["argv"][-1] and LESSON_BODY not in calls[0]["argv"][-1]
    assert all("L-0001" not in e["ids"] for e in events if e["type"] == "lessons_injected")
    assert _lesson_of(repo, "L-0001")["expired"] is True
