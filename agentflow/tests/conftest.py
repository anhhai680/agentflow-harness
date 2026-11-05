"""Shared fixtures: `devtools/` on sys.path, the real repo root, and a config builder."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

DEVTOOLS = Path(__file__).resolve().parents[2]
AGENTFLOW = DEVTOOLS / "agentflow"
REPO = DEVTOOLS.parent

if str(DEVTOOLS) not in sys.path:
    sys.path.insert(0, str(DEVTOOLS))


def deep_merge(base: Any, patch: Any) -> Any:
    """Merge `patch` into `base`: dicts merge, anything else replaces, `None` deletes a key."""
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        elif key in out:
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


@pytest.fixture
def repo_root() -> Path:
    return REPO


@pytest.fixture
def tmp_cfg(tmp_path: Path) -> Callable[..., Path]:
    """Return `make(patch)`: write the shipped config with `patch` applied, return its path.

    `patch` is a dict (deep-merged, `None` deletes a key) or a callable that mutates the
    parsed YAML in place (needed to edit list items). The config lands in
    `<tmp>/agentflow/config.yaml` with `prompts/` and `.claude/skills/` symlinked in, so
    `<tmp>` works as the repo root for existence checks.
    """

    def make(patch: dict | Callable[[dict], None] | None = None, name: str = "config.yaml") -> Path:
        data = yaml.safe_load((AGENTFLOW / "config.yaml").read_text(encoding="utf-8"))
        if callable(patch):
            patch(data)
        elif patch:
            data = deep_merge(data, patch)
        pkg = tmp_path / "agentflow"
        pkg.mkdir(exist_ok=True)
        if not (pkg / "prompts").exists():
            (pkg / "prompts").symlink_to(AGENTFLOW / "prompts", target_is_directory=True)
        claude = tmp_path / ".claude"
        claude.mkdir(exist_ok=True)
        if not (claude / "skills").exists():
            (claude / "skills").symlink_to(REPO / ".claude" / "skills", target_is_directory=True)
        target = pkg / name
        target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return target

    return make


class FakeAgent:
    """Handle on `tests/fake_agent.py`: scripts its acts per step key and reads back its call log."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.script_path = tmp_path / "fake_script.json"
        self.calls_path = tmp_path / "fake_calls.jsonl"
        self.command = [sys.executable, str(AGENTFLOW / "tests" / "fake_agent.py")]
        self.script_path.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("AGENTFLOW_FAKE_SCRIPT", str(self.script_path))
        monkeypatch.setenv("AGENTFLOW_FAKE_CALLS", str(self.calls_path))

    def script(self, acts_by_step: dict[str, list[dict]]) -> None:
        """Set `{step_key: [act, ...]}`; each call for a key consumes the next act."""
        self.script_path.write_text(json.dumps(acts_by_step), encoding="utf-8")
        cursor = self.script_path.with_name(self.script_path.name + ".cursor")
        cursor.unlink(missing_ok=True)

    def calls(self) -> list[dict]:
        if not self.calls_path.exists():
            return []
        return [json.loads(line) for line in self.calls_path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeAgent:
    return FakeAgent(tmp_path, monkeypatch)


def run_git(cwd: Path, *args: str) -> str:
    """Run `git <args>` in `cwd`, return stdout, raise on failure."""
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def git() -> Callable[..., str]:
    return run_git


@pytest.fixture
def tmp_git(tmp_path: Path) -> Path:
    """A real git repo with one commit holding a few of the guarded files."""
    root = tmp_path / "gitrepo"
    root.mkdir()
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "config", "user.name", "Test")
    run_git(root, "config", "user.email", "test@example.com")
    run_git(root, "config", "commit.gpgsign", "false")
    (root / "CONSTRAINTS.md").write_text("rules\n", encoding="utf-8")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def cfg():
    """The shipped config, loaded."""
    from agentflow.config import load_config

    return load_config(AGENTFLOW / "config.yaml")


@pytest.fixture
def repo(tmp_git: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real git repo that is also the working directory, so CLI commands find it."""
    monkeypatch.chdir(tmp_git)
    return tmp_git


@pytest.fixture
def run(repo: Path, cfg):
    """A freshly created `feature` run for module `crm` inside `repo`."""
    from agentflow.state import Run

    return Run.create(cfg, repo, "feature", {"module": "crm"})


TEST_GATES = {
    "ok": {"run": "echo fine"},
    "fail": {"run": "echo 'boom {module} line'; exit 1"},
    "absent": {"run": "echo never", "when_exists": "no-such-dir"},
}


# The narrow Bash rules the flow repo's agents (and so the test commands the runner runs for them) get.
FLOW_SETTINGS = {"permissions": {"allow": ["Bash(true)", "Bash(false)", "Bash(test *)", "Bash(touch *)"],
                                 "deny": ["Bash(touch .claude/*)"]}}


class FlowRepo:
    """A tmp git repo (branches `main`, `next`, bare `origin`) wired to the fake agent.

    `configure(patch)` reloads the shipped config with the fake agent as `claude`, no notifiers,
    the `TEST_GATES` added, and `patch` deep-merged on top. `new_run()` starts a `feature` run for
    module `crm`; `executor()` builds an `Executor` for it. The repo ignores the runs root and the agent
    workspaces, as the real one does, so run files never show up in a step's diff.
    """

    def __init__(self, root: Path, origin: Path, fake: FakeAgent, make_cfg: Callable[..., Path]) -> None:
        self.root = root
        self.origin = origin
        self.fake = fake
        self._make_cfg = make_cfg
        self.cfg = self.configure()
        self.run = None

    def configure(self, patch: dict | None = None, autopilot: bool = False, batch: bool = False):
        """`autopilot=False` (the default) keeps a human at every phase and no `ci` step, so a test pins one
        stop or approval at a time. `autopilot=True` is the workflow as shipped. `batch=False` (the default)
        drops the build step's `batch`, so a test's file-less plan tasks each get a session and a review."""
        from agentflow.config import load_config

        base = {
            "project": {"fork_from": "next"},  # the branch this fixture's repo works on, whatever config.yaml forks from
            "agents": {"claude": {"command": self.fake.command}},
            "context": {"default_window_tokens": 200000},  # the fake agent scripts `usage_pct` against 200K
            "hitl": {"notify": []},
            "gates": TEST_GATES,
        }

        def build(data: dict) -> None:
            if not autopilot:
                for name, phase in data["phases"].items():
                    phase["needs_approval"] = name != list(data["phases"])[-1]
                    phase.pop("resolve", None)
                    phase["steps"] = [step for step in phase["steps"] if step.get("type") != "ci"]
            if not batch:
                for phase in data["phases"].values():
                    for step in phase["steps"]:
                        if isinstance(step, dict):
                            step.pop("batch", None)
            merged = deep_merge(data, deep_merge(base, patch or {}))
            data.clear()
            data.update(merged)

        self.cfg = load_config(self._make_cfg(build))
        return self.cfg

    def script(self, acts: dict[str, list[dict]]) -> None:
        self.fake.script(acts)

    def calls(self) -> list[dict]:
        return self.fake.calls()

    def new_run(self, params: dict | None = None):
        from agentflow.state import Run

        self.run = Run.create(self.cfg, self.root, "feature", params or {"module": "crm"})
        return self.run

    def executor(self, run=None, **kwargs: Any):
        from agentflow.agents import make_agents
        from agentflow.conditions import make_ctx
        from agentflow.lanes import Executor

        run = run or self.run or self.new_run()
        ctx = make_ctx(self.cfg, self.root, run.state["pipeline"], run.state["params"], run)
        return Executor(self.cfg, run, make_agents(self.cfg), ctx, {}, **kwargs)

    def events(self, run, etype: str) -> list[dict]:
        return [e for e in run.events.read() if e["type"] == etype]

    def head(self) -> str:
        return run_git(self.root, "rev-parse", "HEAD").strip()


@pytest.fixture
def flow_repo(tmp_path: Path, fake: FakeAgent, tmp_cfg: Callable[..., Path], monkeypatch: pytest.MonkeyPatch) -> FlowRepo:
    origin = tmp_path / "origin.git"
    run_git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    root = tmp_path / "flow"
    root.mkdir()
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "config", "user.name", "Test")
    run_git(root, "config", "user.email", "test@example.com")
    run_git(root, "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text("devtools/agentflow/runs/\ndevtools/agentflow/workspaces/\n__pycache__/\n",
                                    encoding="utf-8")
    (root / "CONSTRAINTS.md").write_text("rules\n", encoding="utf-8")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text(json.dumps(FLOW_SETTINGS, indent=2) + "\n", encoding="utf-8")
    (root / ".claude" / "skills").symlink_to(REPO / ".claude" / "skills", target_is_directory=True)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "initial")
    run_git(root, "branch", "next")
    run_git(root, "remote", "add", "origin", str(origin))
    run_git(root, "push", "-q", "origin", "main", "next")
    run_git(root, "checkout", "-q", "next")
    monkeypatch.chdir(root)
    return FlowRepo(root, origin, fake, tmp_cfg)
