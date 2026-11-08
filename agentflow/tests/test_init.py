"""`agentflow init`: detect, write a sparse pack once, and run the gates on the base branch only when asked."""

from __future__ import annotations

import dataclasses
import io
import os
import shlex
import shutil
import signal
import stat
import time
from pathlib import Path

import pytest
import yaml

from agentflow import detect, doctor, flow, init, layers, pack
from agentflow.config import Gate, load_config
from conftest import PYTHON_CONFIG, run_git

ENGINE_BIN = Path(__file__).resolve().parent / "bin"
SYSTEM_PATH = os.environ.get("PATH", "")
PERMISSION_COMMENT = ("runs are headless: this lets the agent use its tools; deny rules, hooks and agentflow's guards "
                      "still bind. Without it, each gate and the bug lane's test command need a Bash allow rule in "
                      ".claude/settings.json (validate lists them).")


def _script(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def fakebin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fakes first on PATH: a logged-in `claude`, the tests' `gh`, and a `uv` that logs its calls to `<tmp>/uv-calls`.
    `FAKE_UV=fail` fails `uv sync`; `FAKE_UV=hooks` makes it change `.git/config` and plant a hook;
    `FAKE_PYTEST_EXIT` is the exit code of `uv run pytest`."""
    bins = tmp_path / "fakebin"
    bins.mkdir()
    _script(bins / "claude", 'if [ "$1 $2" = "auth status" ]; then exit 0; fi\nexit 1\n')
    (bins / "gh").symlink_to(ENGINE_BIN / "gh")
    calls = shlex.quote(str(tmp_path / "uv-calls"))
    _script(bins / "uv", f"""echo "$*" >> {calls}
if [ "$1" = sync ]; then
  case "$FAKE_UV" in
    fail) echo "uv: cannot sync" >&2; exit 1 ;;
    hooks) git config core.hooksPath x && touch "$(git rev-parse --git-common-dir)/hooks/planted" ;;
  esac
  exit 0
fi
if [ "$1 $2" = "run pytest" ]; then exit "${{FAKE_PYTEST_EXIT:-0}}"; fi
exit 0
""")
    monkeypatch.setenv("PATH", f"{bins}{os.pathsep}{SYSTEM_PATH}")
    for name in ("FAKE_UV", "FAKE_PYTEST_EXIT", "FAKE_GH_FAIL"):
        monkeypatch.delenv(name, raising=False)
    return bins


def _workflow(steps: list[dict]) -> str:
    return yaml.safe_dump({"name": "CI", "on": "push",
                           "jobs": {"test": {"runs-on": "ubuntu-latest", "steps": steps}}}, sort_keys=False)


def set_ci(repo: Path, steps: list[dict], files: dict[str, str] | None = None) -> None:
    """Commit a CI workflow with one job of `steps` (and `files`) on the current branch."""
    for name, text in {".github/workflows/ci.yml": _workflow(steps), **(files or {})}.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "ci")


@pytest.fixture
def toy_repo(tmp_path: Path, fakebin: Path) -> Path:
    """A git repo on `main` with one commit pushed to a bare `origin` (no `remote set-head`), holding a
    pyproject.toml and a CI workflow whose one gate is `pytest -q`."""
    origin = tmp_path / "origin.git"
    run_git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    repo = tmp_path / "toy"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        run_git(repo, "config", key, value)
    (repo / "pyproject.toml").write_text("[project]\nname = 'toy'\n", encoding="utf-8")
    set_ci(repo, [{"name": "Tests", "run": "pytest -q"}])
    run_git(repo, "remote", "add", "origin", str(origin))
    run_git(repo, "push", "-q", "origin", "main")
    return repo.resolve()


def git_status(repo: Path) -> str:
    return run_git(repo, "status", "--porcelain", "--untracked-files=all")


def run_init(repo: Path, *, starter: str | None = None, baseline: bool = False) -> tuple[int, str]:
    out = io.StringIO()
    code = init.init(repo, starter=starter, run_baseline=baseline, out=out)
    return code, out.getvalue()


def pack_file(repo: Path) -> Path:
    return repo / ".agentflow" / "config.yaml"


def written(repo: Path) -> dict:
    return yaml.safe_load(pack_file(repo).read_text(encoding="utf-8"))


def worktrees(repo: Path) -> list[Path]:
    out = run_git(repo, "worktree", "list", "--porcelain")
    return [Path(line.removeprefix("worktree ")) for line in out.splitlines() if line.startswith("worktree ")]


def leftovers(repo: Path) -> list[Path]:
    return [p for p in pack.state_dir(repo).glob("baseline-*") if p.name != "baseline-logs"]


def capture_rows(monkeypatch: pytest.MonkeyPatch) -> list:
    seen: list = []
    real = init.baseline

    def wrapped(*args, **kwargs):
        rows = real(*args, **kwargs)
        seen.extend(rows)
        return rows

    monkeypatch.setattr(init, "baseline", wrapped)
    return seen


def statuses(rows: list) -> dict[str, str]:
    return {row.gate: row.status for row in rows}


def tiny_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The provisional pack's gates time out after the shortest time `gates` allows (one second)."""
    real = doctor.load_config

    def load(*args, **kwargs):
        cfg = real(*args, **kwargs)
        return dataclasses.replace(cfg, defaults=dataclasses.replace(cfg.defaults, gate_timeout_min=0.001))

    monkeypatch.setattr(doctor, "load_config", load)


def no_red(repo: Path) -> bool:
    return all("baseline" not in gate for gate in (written(repo).get("gates") or {}).values())


# --- the pack ---------------------------------------------------------------------------------------------------------

def test_init_writes_only_the_pack(toy_repo):
    code, out = run_init(toy_repo)
    assert code == 0, out
    assert git_status(toy_repo) == "?? .agentflow/config.yaml\n"
    shutil.rmtree(toy_repo / ".agentflow")
    assert git_status(toy_repo) == ""


def test_init_runs_no_gate_without_the_baseline_flag(toy_repo, tmp_path):
    marker = tmp_path / "ran"
    set_ci(toy_repo, [{"name": "Tests", "run": f"touch {marker} # test"}])
    code, out = run_init(toy_repo)
    assert code == 0, out
    assert not marker.exists() and not (tmp_path / "uv-calls").exists()
    assert "baseline not run: pass --baseline to run the detected gates once on `main`" in out
    assert "touch" in written(toy_repo)["gates"]["tests"]["run"]


def test_the_written_pack_loads_back_to_the_rendered_data(toy_repo):
    set_ci(toy_repo, [{"name": "Lint", "run": "ruff check ."}, {"name": "Tests", "run": "cd app\npytest -q\n"}],
           files={"docs/plans/a.md": "", "docs/adr/b.md": ""})
    code, out = run_init(toy_repo)
    assert code == 0, out
    text = pack_file(toy_repo).read_text(encoding="utf-8")
    found = detect.detect(toy_repo)
    base = layers.merge(layers.base_layers(found.starter))[0]
    assert text == init.render_pack(found, base=base)
    multi = "bash -e -c " + shlex.quote("export CI=true\ncd app\npytest -q")
    assert yaml.safe_load(text) == init.pack_data(found, base=base) == {
        "version": 2, "engine": ">=2,<3", "starter": "python-uv", "gates_confirmed": False,
        "agents": {"claude": {"permission_mode": "bypassPermissions"}},
        "project": {"docs": {"decisions": "docs/adr"}},  # docs/plans is the engine default: not written
        "gates": {"lint": {"run": "ruff check ."}, "tests": {"run": multi}},
        "gate_sets": {"verify": ["lint", "tests"], "build": ["lint"]},
    }
    assert "run: |" in text  # a multi-line command is a literal block
    cfg = load_config(pack_file(toy_repo), user=False)
    assert cfg.gates["tests"].run == multi and cfg.gate_sets["setup"] == ["sync"]


SPECS_COMMENT = ("# the Backlog: one module per spec file in project.docs.specs; a new feature card writes a new "
                 "spec\n")


def _detected(**docs: str) -> detect.Detected:
    return detect.Detected(base_branch="main", base_ref=None, starter="python-uv", docs=dict(docs))


def test_a_found_specs_directory_adds_a_specs_work_source_and_a_feature_backlog():
    found = _detected(specs="docs/proposals")
    base = init.base_of(found.starter)
    data = init.pack_data(found, base=base)
    assert data["work_source"] == {"kind": "specs"}
    assert data["dashboard"] == {"backlog": {"module_lane": "feature",
                                             "new_task": {"feature": ["new_module", "after"],
                                                          "bug": ["issue", "task", "after"]}}}
    text = init.render_pack(found, base=base)
    assert SPECS_COMMENT + "work_source:\n  kind: specs\ndashboard:\n  backlog:\n    module_lane: feature\n" in text
    assert yaml.safe_load(text) == data


def test_a_detected_spec_file_pattern_is_written_beside_the_specs_directory():
    found = _detected(specs="docs/proposals", spec_file="AA-{module}.md")
    base = init.base_of(found.starter)
    data = init.pack_data(found, base=base)
    assert data["project"]["docs"] == {"specs": "docs/proposals", "spec_file": "AA-{module}.md"}
    assert data["work_source"] == {"kind": "specs"}
    assert yaml.safe_load(init.render_pack(found, base=base)) == data


def test_no_specs_directory_means_no_work_source_or_backlog():
    found = _detected(plans="docs/plans")
    base = init.base_of(found.starter)
    data = init.pack_data(found, base=base)
    assert "work_source" not in data and "dashboard" not in data
    text = init.render_pack(found, base=base)
    assert "work_source" not in text and "dashboard" not in text


def test_a_specs_directory_that_looks_like_a_reference_adds_no_work_source():
    found = _detected(specs="@specs")  # dropped by `_checked`, so the work source would read the wrong folder
    data = init.pack_data(found, base=init.base_of(found.starter))
    assert "work_source" not in data and "dashboard" not in data


def test_a_base_branch_other_than_main_is_written(toy_repo):
    run_git(toy_repo, "branch", "-m", "main", "trunk")
    run_git(toy_repo, "config", "init.defaultBranch", "trunk")
    code, out = run_init(toy_repo)
    assert code == 0, out
    assert written(toy_repo)["project"] == {"base_branch": "trunk", "fork_from": "trunk"}


def test_the_pack_has_the_permission_line_with_its_comment(toy_repo):
    code, out = run_init(toy_repo)
    assert code == 0, out
    text = pack_file(toy_repo).read_text(encoding="utf-8")
    assert f"# {PERMISSION_COMMENT}\nagents:\n  claude:\n    permission_mode: bypassPermissions\n" in text
    assert text.startswith("#")
    assert "gates_confirmed: false" in text


def test_a_value_that_looks_like_a_reference_is_dropped(toy_repo):
    set_ci(toy_repo, [{"name": "Ref", "run": "@x pytest"},
                      {"name": "Web tests", "working-directory": "@scope/web", "run": "npm test"},
                      {"name": "Tests", "run": "pytest -q"}])
    code, out = run_init(toy_repo)
    assert code == 0, out
    data = written(toy_repo)
    assert data["gates"] == {"tests": {"run": "pytest -q"}}
    assert data["gate_sets"] == {"verify": ["tests"], "build": ["tests"]}
    assert "`ref`" in out and "`web_tests`" in out and "@" in out
    load_config(pack_file(toy_repo), user=False)


def test_init_refuses_an_existing_pack_a_broken_symlink_and_a_symlinked_dir(toy_repo, tmp_path):
    message = "this repository already has a pack: edit it, or remove .agentflow to start over"
    folder = toy_repo / ".agentflow"
    folder.mkdir()
    (folder / "config.yaml").write_text("mine\n", encoding="utf-8")
    code, out = run_init(toy_repo)
    assert code == 1 and message in out and "init" in out
    assert (folder / "config.yaml").read_text(encoding="utf-8") == "mine\n"

    (folder / "config.yaml").unlink()
    (folder / "config.yaml").symlink_to(tmp_path / "nowhere.yaml")
    code, out = run_init(toy_repo, baseline=True)
    assert code == 1 and message in out
    assert not (tmp_path / "nowhere.yaml").exists() and not (tmp_path / "uv-calls").exists()

    (folder / "config.yaml").unlink()
    folder.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    folder.symlink_to(elsewhere, target_is_directory=True)
    code, out = run_init(toy_repo)
    assert code == 1 and "symlink" in out and "init" in out
    assert list(elsewhere.iterdir()) == []


def test_the_starter_flag_overrides_detection(toy_repo):
    code, out = run_init(toy_repo, starter="docs-only")
    assert code == 0, out
    assert written(toy_repo)["starter"] == "docs-only"


def test_an_unknown_starter_flag_is_refused(toy_repo, monkeypatch, capsys):
    code, out = run_init(toy_repo, starter="../defaults")
    assert code == 1 and "init" in out and "unknown starter" in out
    assert not (toy_repo / ".agentflow").exists()
    monkeypatch.chdir(toy_repo)
    assert flow.main(["init", "--starter", "nope"]) == 1
    printed = capsys.readouterr().out
    assert "init" in printed and "unknown starter" in printed and "runner stopped" not in printed


def test_cli_init_in_a_linked_worktree_writes_into_the_main_checkout(toy_repo, tmp_path, monkeypatch, capsys):
    linked = tmp_path / "linked"
    run_git(toy_repo, "worktree", "add", "-q", "-b", "side", str(linked))
    monkeypatch.chdir(linked)
    assert flow.main(["init"]) == 0
    out = capsys.readouterr().out
    assert str(toy_repo) in out
    assert pack_file(toy_repo).is_file() and not (linked / ".agentflow").exists()
    assert flow.main(["init"]) == 1
    out = capsys.readouterr().out
    assert "init" in out and "already has a pack" in out and "runner stopped" not in out


# --- the baseline -----------------------------------------------------------------------------------------------------

def test_baseline_marks_a_failing_gate_red_and_drops_it_from_every_set(toy_repo, monkeypatch):
    rows = capture_rows(monkeypatch)
    set_ci(toy_repo, [{"name": "Style", "run": "test -f nope.cfg # lint"}, {"name": "Tests", "run": "test -f pyproject.toml"}])
    code, out = run_init(toy_repo, baseline=True)
    assert code == 0, out
    assert statuses(rows) == {"sync": "pass", "style": "fail", "tests": "pass"}
    data = written(toy_repo)
    assert data["gates"] == {"style": {"run": "test -f nope.cfg # lint", "baseline": "red"},
                             "tests": {"run": "test -f pyproject.toml"}}
    assert data["gate_sets"] == {"setup": ["sync"], "build": [], "verify": ["tests"]}
    assert load_config(pack_file(toy_repo), user=False).gates["style"].baseline == "red"
    assert "style" in out and "fail" in out


def test_baseline_marks_a_failing_starter_gate_red_with_its_run(toy_repo, monkeypatch):
    rows = capture_rows(monkeypatch)
    set_ci(toy_repo, [{"name": "Hello", "run": "echo hi"}], files={
        "pyproject.toml": "[project]\nname = 'toy'\n[tool.pytest.ini_options]\naddopts = '-q'\n[tool.ruff]\nline-length = 100\n"})
    monkeypatch.setenv("FAKE_PYTEST_EXIT", "1")
    code, out = run_init(toy_repo, baseline=True)
    assert code == 0, out
    assert statuses(rows) == {"sync": "pass", "ruff": "pass", "tests": "fail"}
    data = written(toy_repo)
    assert data["gates"] == {"ruff": {"run": "uv run ruff check ."},
                             "tests": {"run": "uv run pytest -q", "baseline": "red"}}
    assert data["gate_sets"] == {"setup": ["sync"], "build": ["ruff"], "verify": ["ruff"]}


def test_baseline_keeps_red_gates_in_verify_when_nothing_else_is_left(toy_repo):
    set_ci(toy_repo, [{"name": "Tests", "run": "test -f nope.txt"}])
    code, out = run_init(toy_repo, baseline=True)
    assert code == 0, out
    data = written(toy_repo)
    assert data["gates"]["tests"] == {"run": "test -f nope.txt", "baseline": "red"}
    assert data["gate_sets"] == {"setup": ["sync"], "build": [], "verify": ["tests"]}
    assert "every verify gate fails on `main`: runs cannot pass verify until you fix them" in out


def test_a_failing_setup_is_an_error_and_reddens_nothing(toy_repo, monkeypatch):
    rows = capture_rows(monkeypatch)
    monkeypatch.setenv("FAKE_UV", "fail")
    set_ci(toy_repo, [{"name": "Tests", "run": "test -f nope.txt"}])
    code, out = run_init(toy_repo, baseline=True)
    assert code == 0, out
    assert statuses(rows) == {"sync": "error", "tests": "not run"}
    assert "setup failed" in rows[0].note
    assert no_red(toy_repo)


@pytest.mark.parametrize("run", ["exit 127", "sleep 5"])
def test_an_environment_failure_is_an_error_not_red(toy_repo, monkeypatch, run):
    rows = capture_rows(monkeypatch)
    tiny_timeout(monkeypatch)
    set_ci(toy_repo, [{"name": "Tests", "run": f"{run} # test"}])
    started = time.monotonic()
    code, out = run_init(toy_repo, starter="docs-only", baseline=True)  # no setup gate to share the tiny timeout
    assert code == 0, out
    assert statuses(rows) == {"tests": "error"}, out
    assert no_red(toy_repo)
    assert time.monotonic() - started < 5 + 20  # the sleep was cut short by the timeout


def test_a_gate_with_a_missing_program_is_not_run(toy_repo, monkeypatch):
    rows = capture_rows(monkeypatch)
    set_ci(toy_repo, [{"name": "Tests", "run": "nosuchtool-xyz --test"}])
    code, out = run_init(toy_repo, starter="docs-only", baseline=True)
    assert code == 0, out
    assert statuses(rows) == {"tests": "not run"}
    assert "nosuchtool-xyz" in rows[0].note
    assert no_red(toy_repo)


def test_a_skipped_gate_is_not_red(toy_repo):
    cfg = load_config(PYTHON_CONFIG, user=False)
    cfg = dataclasses.replace(cfg, gates={"opt": Gate("opt", "false", when_exists="nope"), "ok": Gate("ok", "true")},
                              gate_sets={"setup": [], "build": ["opt"], "verify": ["opt", "ok"]})
    rows = init.baseline(cfg, toy_repo, "main", set(), io.StringIO())
    assert statuses(rows) == {"opt": "skipped", "ok": "pass"}
    assert worktrees(toy_repo) == [toy_repo]


def test_baseline_runs_on_the_base_branch_not_the_working_tree(toy_repo, monkeypatch):
    rows = capture_rows(monkeypatch)
    set_ci(toy_repo, [{"name": "Tests", "run": "test -f on_main.txt"}], files={"on_main.txt": "x\n"})
    run_git(toy_repo, "checkout", "-q", "-b", "wip")
    run_git(toy_repo, "rm", "-q", "on_main.txt")
    run_git(toy_repo, "commit", "-q", "-m", "drop it")
    code, out = run_init(toy_repo, baseline=True)
    assert code == 0, out
    assert statuses(rows)["tests"] == "pass"
    assert not (toy_repo / "on_main.txt").exists()


def test_baseline_runs_without_the_callers_virtualenv(toy_repo, monkeypatch, tmp_path):
    rows = capture_rows(monkeypatch)
    for name in ("VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH"):
        monkeypatch.setenv(name, str(tmp_path / "venv"))
    set_ci(toy_repo, [{"name": "Tests", "run": 'test -z "$VIRTUAL_ENV$CONDA_PREFIX$PYTHONPATH"'}])
    code, out = run_init(toy_repo, baseline=True)
    assert code == 0, out
    assert statuses(rows)["tests"] == "pass"


def test_baseline_leaves_no_worktree_even_when_a_gate_times_out(toy_repo, monkeypatch):
    rows = capture_rows(monkeypatch)
    tiny_timeout(monkeypatch)
    set_ci(toy_repo, [{"name": "Tests", "run": "sleep 5 # test"}])
    code, out = run_init(toy_repo, starter="docs-only", baseline=True)
    assert code == 0, out
    assert statuses(rows) == {"tests": "error"} and "timed out" in rows[-1].note, out
    assert worktrees(toy_repo) == [toy_repo] and leftovers(toy_repo) == []
    assert git_status(toy_repo) == "?? .agentflow/config.yaml\n"


def test_baseline_cleans_up_its_own_leftover_and_keeps_other_worktrees(toy_repo, tmp_path):
    state = pack.state_dir(toy_repo)
    state.mkdir(parents=True, exist_ok=True)
    leftover = state / "baseline-old"
    other = tmp_path / "other-worktree"
    gone = tmp_path / "gone-worktree"  # someone else's worktree whose directory is gone: never pruned by init
    for path in (leftover, other, gone):
        run_git(toy_repo, "worktree", "add", "-q", "--detach", str(path), "main")
    shutil.rmtree(gone)
    code, out = run_init(toy_repo, baseline=True)
    assert code == 0, out
    assert sorted(worktrees(toy_repo)) == sorted([toy_repo, other.resolve(), gone.resolve()])
    assert not leftover.exists() and leftovers(toy_repo) == []
    assert "prune" not in out


def test_a_leftover_git_will_not_remove_because_it_is_gone_is_pruned_and_said(toy_repo, monkeypatch):
    state = pack.state_dir(toy_repo)
    state.mkdir(parents=True, exist_ok=True)
    leftover = state / "baseline-old"
    run_git(toy_repo, "worktree", "add", "-q", "--detach", str(leftover), "main")
    shutil.rmtree(leftover)
    real = init._git

    def refusing(repo, *args, **kwargs):
        if args[:2] == ("worktree", "remove") and Path(args[-1]) == leftover:
            return 128, f"fatal: '{leftover}' is not a working tree"
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(init, "_git", refusing)
    code, out = run_init(toy_repo, baseline=True)
    assert code == 0, out
    assert "git worktree prune" in out
    assert worktrees(toy_repo) == [toy_repo]


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_an_interrupted_baseline_writes_no_pack(toy_repo, monkeypatch, sig):
    set_ci(toy_repo, [{"name": "Tests", "run": "test -f pyproject.toml"}])
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    real = init.run_gates

    def interrupting(cfg, names, *args, **kwargs):
        if names == ["sync"]:
            return real(cfg, names, *args, **kwargs)
        handler = signal.getsignal(sig)
        assert callable(handler) and handler not in before.values(), "no handler: the signal would end the test"
        os.kill(os.getpid(), sig)
        time.sleep(5)  # the handler raises KeyboardInterrupt here
        pytest.fail("the signal did not interrupt the baseline")

    monkeypatch.setattr(init, "run_gates", interrupting)
    code, out = run_init(toy_repo, baseline=True)
    assert code != 0 and "interrupted" in out
    assert not (toy_repo / ".agentflow").exists()
    assert worktrees(toy_repo) == [toy_repo] and leftovers(toy_repo) == []
    assert {s: signal.getsignal(s) for s in before} == before


def test_a_pack_created_during_the_baseline_is_not_overwritten(toy_repo, monkeypatch):
    real = init.baseline

    def racing(*args, **kwargs):
        (toy_repo / ".agentflow").mkdir()
        pack_file(toy_repo).write_text("theirs\n", encoding="utf-8")
        return real(*args, **kwargs)

    monkeypatch.setattr(init, "baseline", racing)
    code, out = run_init(toy_repo, baseline=True)
    assert code == 1 and "already has a pack" in out
    assert pack_file(toy_repo).read_text(encoding="utf-8") == "theirs\n"


def test_init_reports_changed_git_config_and_hooks(toy_repo, monkeypatch):
    monkeypatch.setenv("FAKE_UV", "hooks")
    code, out = run_init(toy_repo, baseline=True)
    assert code == 0, out
    assert "core.hookspath=x" in out
    assert "planted" in out


@pytest.mark.usefixtures("fakebin")
def test_init_on_an_unborn_repo_writes_a_pack_and_skips_the_baseline(tmp_path):
    repo = tmp_path / "fresh"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    (repo / "pyproject.toml").write_text("[project]\nname = 'fresh'\n", encoding="utf-8")
    code, out = run_init(repo, baseline=True)
    assert code == 0, out
    assert "baseline not run" in out and "no commits" in out
    assert not (tmp_path / "uv-calls").exists()
    assert written(repo)["starter"] == "python-uv"


def test_init_prints_a_bash_allow_rule_for_the_bug_lane(toy_repo):
    set_ci(toy_repo, [{"name": "Tests", "run": "pytest -q tests"}])
    code, out = run_init(toy_repo)
    assert code == 0, out
    assert "Bash(pytest -q *)" in out
    shutil.rmtree(toy_repo / ".agentflow")
    set_ci(toy_repo, [{"name": "Hello", "run": "echo hi"}], files={
        "pyproject.toml": "[project]\nname = 'toy'\n[tool.pytest.ini_options]\naddopts = '-q'\n"})
    code, out = run_init(toy_repo)
    assert code == 0, out
    assert "Bash(uv run pytest *)" in out


def test_the_baseline_checkout_runs_no_hook(toy_repo, tmp_path):
    marker = tmp_path / "hook-ran"
    _script(toy_repo / ".git" / "hooks" / "post-checkout", f"touch {shlex.quote(str(marker))}\n")
    set_ci(toy_repo, [{"name": "Tests", "run": "test -f pyproject.toml"}])
    code, out = run_init(toy_repo, baseline=True)
    assert code == 0, out
    assert not marker.exists()


# --- fix round 1 ------------------------------------------------------------------------------------------------------

def _cfg_with(gates: dict[str, str]):
    cfg = load_config(PYTHON_CONFIG, user=False)
    return dataclasses.replace(cfg, gates={n: Gate(n, run) for n, run in gates.items()},
                               gate_sets={"setup": [], "build": [], "verify": list(gates)})


def test_a_runner_that_could_not_start_its_tool_or_a_signal_death_is_an_error_not_red(toy_repo):
    cfg = _cfg_with({"spawn": "echo 'error: Failed to spawn: `nosuchtool`' >&2; exit 2",
                     "shell_said": "echo 'sh: 1: nosuchtool: not found'; exit 1",
                     "bash_said": "echo 'bash: nosuchtool: command not found'; exit 1",
                     "killed": "exit 137", "plain": "exit 1"})
    rows = init.baseline(cfg, toy_repo, "main", set(), io.StringIO())
    assert statuses(rows) == {"spawn": "error", "shell_said": "error", "bash_said": "error", "killed": "error",
                              "plain": "fail"}


@pytest.mark.parametrize("run,rule", [
    ("python -m pytest -q", "Bash(python -m pytest *)"),
    ("python3 -m pytest", "Bash(python3 -m pytest *)"),
    ("node --test", "Bash(node --test *)"),
    ("uv --frozen run pytest", "Bash(uv --frozen run pytest *)"),
    ('git -c core.whitespace=-blank-at-eol diff --check "$(git hash-object -t tree /dev/null)"',
     "Bash(git -c core.whitespace=-blank-at-eol diff *)"),
    ("uv run pytest -q", "Bash(uv run pytest *)"),
    ("npm run test", "Bash(npm run test *)"),
    ("pytest -q tests", "Bash(pytest -q *)"),
    ("ruff check .", "Bash(ruff check *)"),
    ("cd app && pytest -q", "Bash(pytest -q *)"),
    ("python script.py", None), ("python -X dev -m pytest", None), ("node test.js", None), ("node", None),
    ("git", None), ("git --no-pager", None), ("uv --frozen", None), ("bash -e -c 'pytest'", None),
    ("sh run-tests.sh", None), ("test -f x", None)])
def test_the_bash_allow_rule_is_never_a_bare_interpreter_or_dispatcher(run, rule):
    assert init._allow_rule(run) == rule


def test_the_docs_only_starter_gets_a_narrow_git_rule(toy_repo):
    set_ci(toy_repo, [{"name": "Hello", "run": "echo hi"}])
    code, out = run_init(toy_repo, starter="docs-only")
    assert code == 0, out
    assert "Bash(git -c core.whitespace=-blank-at-eol diff *)" in out and "Bash(git *)" not in out


def test_a_second_baseline_while_one_runs_refuses_and_leaves_the_others_worktree(toy_repo):
    import fcntl

    state = pack.state_dir(toy_repo)
    state.mkdir(parents=True, exist_ok=True)
    live = state / "baseline-live"  # the other init's worktree, in use
    run_git(toy_repo, "worktree", "add", "-q", "--detach", str(live), "main")
    with open(state / "init.lock", "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, out = run_init(toy_repo, baseline=True)
    assert code == 1 and "init" in out and "another" in out
    assert live.is_dir() and live.resolve() in worktrees(toy_repo)
    assert not (toy_repo / ".agentflow").exists()
    code, out = run_init(toy_repo, baseline=True)  # the lock is free again
    assert code == 0, out


# --- final review fixes -----------------------------------------------------------------------------------------------

def test_baseline_runs_the_branch_not_a_tag_of_the_same_name(toy_repo, monkeypatch):
    rows = capture_rows(monkeypatch)
    set_ci(toy_repo, [{"name": "Tests", "run": "test -f on_main.txt"}], files={"on_main.txt": "x\n"})
    run_git(toy_repo, "checkout", "-q", "-b", "side")
    run_git(toy_repo, "rm", "-q", "on_main.txt")
    run_git(toy_repo, "commit", "-q", "-m", "not on main")
    run_git(toy_repo, "tag", "main")
    run_git(toy_repo, "checkout", "-q", "main")
    code, out = run_init(toy_repo, baseline=True)
    assert code == 0, out
    assert statuses(rows)["tests"] == "pass"
    assert "a worktree of `main`" in out and "read from `main`" in out and "refs/heads" not in out


def test_a_docker_daemon_that_is_down_means_docker_gates_are_not_run(toy_repo, fakebin, monkeypatch):
    rows = capture_rows(monkeypatch)
    _script(fakebin / "docker", "exit 1\n")
    set_ci(toy_repo, [{"name": "Tests", "run": "docker compose run tests"}])
    code, out = run_init(toy_repo, baseline=True)
    assert code == 0, out
    assert statuses(rows)["tests"] == "not run" and "docker" in rows[-1].note
    assert no_red(toy_repo)


def test_baseline_drops_the_callers_venv_from_path_and_git_variables(toy_repo, tmp_path, monkeypatch):
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    monkeypatch.setenv("VIRTUAL_ENV", str(venv_bin.parent))
    monkeypatch.setenv("PATH", f"{venv_bin}{os.pathsep}{os.environ['PATH']}")
    for name, value in (("GIT_DIR", toy_repo / ".git"), ("GIT_WORK_TREE", toy_repo),
                        ("GIT_INDEX_FILE", toy_repo / ".git" / "index")):
        monkeypatch.setenv(name, str(value))
    cfg = _cfg_with({"path": f'case ":$PATH:" in *":{venv_bin}:"*) exit 1 ;; esac',
                     "git_vars": 'test -z "$GIT_DIR$GIT_WORK_TREE$GIT_INDEX_FILE"'})
    rows = init.baseline(cfg, toy_repo, "refs/heads/main", set(), io.StringIO())
    assert statuses(rows) == {"path": "pass", "git_vars": "pass"}
    assert git_status(toy_repo) == ""


def test_terminal_escapes_from_the_repository_are_not_printed(toy_repo, monkeypatch, capsys):
    set_ci(toy_repo, [{"name": "Tests", "run": "\x1b[1Atool --test"}])
    code, out = run_init(toy_repo)
    assert code == 0, out
    assert "\x1b" not in out and "\\x1b[1Atool" in out
    text = doctor.render([doctor.Check("gate:x\x1b[2J", "fail", "`\x1b[1A` is not on PATH", "install `\x1b[1A`")])
    assert "\x1b" not in text


def test_the_baseline_names_its_log_folder_when_a_gate_fails_or_errs(toy_repo):
    set_ci(toy_repo, [{"name": "Tests", "run": "test -f pyproject.toml"}])
    code, out = run_init(toy_repo, baseline=True)
    assert code == 0, out
    assert "baseline-logs" not in out.split("baseline:\n")[-1]
    shutil.rmtree(toy_repo / ".agentflow")
    set_ci(toy_repo, [{"name": "Tests", "run": "test -f nope.txt"}])
    code, out = run_init(toy_repo, baseline=True)
    assert code == 0, out
    assert f"logs: {pack.state_dir(toy_repo) / 'baseline-logs'}" in out
