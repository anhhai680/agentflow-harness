"""Shared fixtures: the repo root on sys.path, this checkout's root, the fixture packs and a config builder."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

ENGINE = Path(__file__).resolve().parents[1]
ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT
PACKS = ENGINE / "tests" / "fixtures" / "packs"
PYTHON_PACK = PACKS / "python"
PYTHON_CONFIG = PYTHON_PACK / ".agentflow" / "config.yaml"  # the suite's pack: the shipped config's shape, a toy's values
PYTHON_SKILLS = PYTHON_PACK / ".claude" / "skills"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

collect_ignore_glob = ["fixtures/*"]  # the fixture packs' own tests belong to their toy repositories, not this suite


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


@pytest.fixture(autouse=True)
def _state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test's default state dir (`pack.state_dir`) is under its own tmp dir, never `~/.local/state`."""
    home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_STATE_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _no_keepawake(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test run starts a real sleep inhibitor (`keepawake`); `test_keepawake.py` puts the real lookup back.
    A stub rather than `AGENTFLOW_NO_KEEPAWAKE`, which would reach every agent's environment."""
    from agentflow import keepawake

    monkeypatch.setattr(keepawake, "inhibitor_argv", lambda pid, platform=None: None)


@pytest.fixture(autouse=True)
def _config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No test reads the developer's own user file (`layers.user_config_path`): its config home is a tmp dir."""
    home = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _claude_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No test reads the developer's own Claude settings (`doctor.claude_config_dir`): the dir is an empty tmp dir."""
    home = tmp_path / "claude-config"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return home


def prime(u: float, resets_in: int = 3600, refused: bool = False) -> None:
    """Record a `five_hour` reading of `u` in the usage window file, for the account of this test's
    `CLAUDE_CONFIG_DIR`, resetting `resets_in` seconds from now."""
    from agentflow import governor

    reading = {"utilization": u, "resets_at": int(time.time()) + resets_in, "refused": refused}
    governor.WindowFile().record(governor.account_key(os.environ.get("CLAUDE_CONFIG_DIR")), {"five_hour": reading},
                                 time.time())


def release() -> None:
    """Record a `five_hour` reading of 0 for a period 5 h ahead: later than any `prime` that resets sooner, so it
    replaces that reading and clears the window at once."""
    from agentflow import governor

    reading = {"utilization": 0.0, "resets_at": int(time.time()) + 5 * 3600, "refused": False}
    governor.WindowFile().record(governor.account_key(os.environ.get("CLAUDE_CONFIG_DIR")), {"five_hour": reading},
                                 time.time())


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """The governor polls every 0.01 s; `FRESH_S` and `READY_MAX_S` keep their values."""
    from agentflow import governor

    monkeypatch.setattr(governor, "POLL_S", 0.01)


@pytest.fixture
def repo_root() -> Path:
    return REPO


@pytest.fixture
def tmp_cfg(tmp_path: Path) -> Callable[..., Path]:
    """Return `make(patch)`: write the `python` pack's config with `patch` applied, return its path.

    `patch` is a dict (deep-merged, `None` deletes a key) or a callable that mutates the parsed YAML in place
    (needed to edit list items). The config lands in `<tmp>/.agentflow/config.yaml` beside an empty `prompts/` (a
    test adds an override), with `<tmp>/.claude/skills/` linked to the pack's skill stubs, so `<tmp>` works as the
    repo root for existence checks.
    """

    def make(patch: dict | Callable[[dict], None] | None = None, name: str = "config.yaml") -> Path:
        data = yaml.safe_load(PYTHON_CONFIG.read_text(encoding="utf-8"))
        if callable(patch):
            patch(data)
        elif patch:
            data = deep_merge(data, patch)
        pkg = tmp_path / ".agentflow"
        pkg.mkdir(exist_ok=True)
        (pkg / "prompts").mkdir(exist_ok=True)  # the pack's own prompts: empty, so the engine's defaults resolve
        claude = tmp_path / ".claude"
        claude.mkdir(exist_ok=True)
        if not (claude / "skills").exists():
            (claude / "skills").symlink_to(PYTHON_SKILLS, target_is_directory=True)
        target = pkg / name
        target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return target

    return make


class FakeAgent:
    """Handle on `tests/fake_agent.py`: scripts its acts per step key and reads back its call log."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.script_path = tmp_path / "fake_script.json"
        self.calls_path = tmp_path / "fake_calls.jsonl"
        self.command = [sys.executable, str(ENGINE / "tests" / "fake_agent.py")]
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
    """The `python` pack's config, loaded."""
    from agentflow.config import load_config

    return load_config(PYTHON_CONFIG)


@pytest.fixture
def pack_repo_python(tmp_path: Path) -> Path:
    """The `python` fixture pack copied into a fresh git repo of its own (one commit on `main`)."""
    root = tmp_path / "python"
    shutil.copytree(PYTHON_PACK, root, ignore=shutil.ignore_patterns("__pycache__"))
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "config", "user.name", "Test")
    run_git(root, "config", "user.email", "test@example.com")
    run_git(root, "config", "commit.gpgsign", "false")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "initial")
    return root


SPARSE_PACK = {  # a pack that states only what the engine defaults can't know: its gates and its permission line
    "version": 2,
    "engine": ">=3,<4",
    "gates": {"unit": {"run": "true"}},
    "gate_sets": {"setup": [], "build": ["unit"], "verify": ["unit"]},
    "agents": {"claude": {"permission_mode": "bypassPermissions"}},
}


@pytest.fixture
def sparse_repo(tmp_path: Path) -> Path:
    """A tmp git repo on `main` with one commit holding a sparse pack (`SPARSE_PACK`) and nothing else of agentflow's:
    no skills, no prompts, so the engine defaults, its bundled skills and its prompts carry every run."""
    root = tmp_path / "sparse"
    root.mkdir()
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "config", "user.name", "Test")
    run_git(root, "config", "user.email", "test@example.com")
    run_git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    (root / ".agentflow").mkdir()
    (root / ".agentflow" / "config.yaml").write_text(yaml.safe_dump(SPARSE_PACK, sort_keys=False), encoding="utf-8")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "initial")
    return root


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


def with_doubt(data: dict) -> None:
    """Add the doubt review to the `python` pack's config: a reconcile step and a reviewer in `define` (before
    `spec_approval`) and in `plan` (after `plan`). A `configure(edit=...)` or `tmp_cfg` callable. The pack's own file
    stays as it is, because it pins the `explain` goldens."""
    data["skills"]["map"]["doubt"] = "agentflow-doubt"

    def pair(prefix: str, reconcile_skill: str, review_prompt: str, output: str, message: str, when: str | None) -> list[dict]:
        review = {"id": f"{prefix}_review", "lanes": ["feature"], "skill": "agentflow-doubt", "role": "review",
                  "prompt": review_prompt, "blocks_on_findings": True}
        if when:
            review["when"] = when
        return [{"id": f"{prefix}_reconcile", "lanes": ["feature"], "skill": reconcile_skill, "role": "plan",
                 "prompt": "reconcile.md", "output": output, "reconciles": True, "commit": True, "message": message},
                review]

    steps = data["phases"]["define"]["steps"]
    at = next(i for i, step in enumerate(steps) if step.get("id") == "spec_approval")
    steps[at:at] = pair("spec", "domain-modeling", "spec_review.md", "docs/specs/SPEC-{module}.md",
                        "Revise SPEC-{module} after its review", "spec_awaits_approval")
    steps = data["phases"]["plan"]["steps"]
    plan = next(step for step in steps if step.get("id") == "plan")
    at = steps.index(plan) + 1
    steps[at:at] = pair("plan", "writing-plans", "plan_review.md", plan["output"],
                        "Revise the {module} plan after its review", None)


class FlowRepo:
    """A tmp git repo (branches `main`, `next`, bare `origin`) wired to the fake agent.

    `configure(patch)` reloads the `python` pack's config with the fake agent as `claude`, no notifiers,
    the `TEST_GATES` added, and `patch` deep-merged on top. `new_run()` starts a `feature` run for
    module `crm`; `executor()` builds an `Executor` for it. The runs root and the agent workspaces are in
    the state dir, outside the repo (`pack.state_dir`), so run files never show up in a step's diff.
    """

    def __init__(self, root: Path, origin: Path, fake: FakeAgent, make_cfg: Callable[..., Path]) -> None:
        self.root = root
        self.origin = origin
        self.fake = fake
        self._make_cfg = make_cfg
        self.cfg = self.configure()
        self.run = None

    def configure(self, patch: dict | None = None, autopilot: bool = False, batch: bool = False,
                  edit: Callable[[dict], None] | None = None):
        """`autopilot=False` (the default) keeps a human at every phase and no `ci` step, so a test pins one
        stop or approval at a time: no phase resolves its stops and the runner takes no recommended answer. `autopilot=True` is the workflow as shipped. `batch=False` (the default)
        drops the build step's `batch`, so a test's file-less plan tasks each get a session and a review.
        `edit` changes the config last, after the patch, for a change a `patch` mapping cannot say (a step list)."""
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
                data.setdefault("autonomy", {})["answer_recommended"] = False
            if not batch:
                for phase in data["phases"].values():
                    for step in phase["steps"]:
                        if isinstance(step, dict):
                            step.pop("batch", None)
            merged = deep_merge(deep_merge(data, base), patch or {})  # so `None` in `patch` deletes any key
            data.clear()
            data.update(merged)
            if edit is not None:
                edit(data)

        self.cfg = load_config(self._make_cfg(build))
        return self.cfg

    def install_pack(self) -> Path:
        """Copy the configured config to `<root>/.agentflow/config.yaml` (git-excluded), so a CLI call or a
        dashboard action that names no `--config` finds a pack. A copy: `self.cfg.path` stays the run's own file."""
        target = self.root / ".agentflow" / "config.yaml"
        (target.parent / "prompts").mkdir(parents=True, exist_ok=True)
        target.write_text(Path(self.cfg.path).read_text(encoding="utf-8"), encoding="utf-8")
        exclude = self.root / ".git" / "info" / "exclude"
        if "/.agentflow/" not in exclude.read_text(encoding="utf-8"):
            with exclude.open("a", encoding="utf-8") as handle:
                handle.write("/.agentflow/\n")
        return target

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


def land_on_origin(flow_repo: FlowRepo, tmp_path: Path, files: dict[str, str], fetch: bool = True) -> None:
    """Merge `files` (`{path: text}`) into origin's `fork_from` the way a merged pull request does: from another
    clone, a fresh one per call. With `fetch`, the checkout then fetches origin, so `origin/<fork_from>` has them."""
    clone = Path(tempfile.mkdtemp(dir=tmp_path)) / "merger"
    run_git(tmp_path, "clone", "-q", str(flow_repo.origin), str(clone))
    for key, value in (("user.name", "Merger"), ("user.email", "merger@example.com"), ("commit.gpgsign", "false")):
        run_git(clone, "config", key, value)
    run_git(clone, "checkout", "-q", flow_repo.cfg.project.fork_from)
    for rel, text in files.items():
        target = clone / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    run_git(clone, "add", "-A")
    run_git(clone, "commit", "-q", "-m", "merge pull request")
    run_git(clone, "push", "-q", "origin", flow_repo.cfg.project.fork_from)
    if fetch:
        run_git(flow_repo.root, "fetch", "-q", "origin")


def brief_ready(flow_repo: FlowRepo, run, files: dict[str, str] | None = None) -> str:
    """Make `run`'s brief readable at a commit, as a real run's fork commit is: replace the `.claude/skills` symlink
    (to a folder outside the repository) with a real copy of its target, write `files` ({path: text}), commit all of it
    (`.agentflow/` too) and record that commit as the run's `base_sha`. Returns the commit."""
    skills = flow_repo.root / ".claude" / "skills"
    if skills.is_symlink():
        target = skills.resolve()
        skills.unlink()
        shutil.copytree(target, skills)
    for rel, text in (files or {}).items():
        path = flow_repo.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    run_git(flow_repo.root, "add", "-A")
    for rel in files or {}:
        run_git(flow_repo.root, "add", "-f", rel)
    run_git(flow_repo.root, "commit", "-q", "--allow-empty", "-m", "brief sources")
    sha = run_git(flow_repo.root, "rev-parse", "HEAD").strip()
    run.emit("run_updated", base_sha=sha)
    return sha


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
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (root / "CONSTRAINTS.md").write_text("rules\n", encoding="utf-8")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text(json.dumps(FLOW_SETTINGS, indent=2) + "\n", encoding="utf-8")
    (root / ".claude" / "skills").symlink_to(PYTHON_SKILLS, target_is_directory=True)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "initial")
    run_git(root, "branch", "next")
    run_git(root, "remote", "add", "origin", str(origin))
    run_git(root, "push", "-q", "origin", "main", "next")
    run_git(root, "checkout", "-q", "next")
    monkeypatch.chdir(root)
    return FlowRepo(root, origin, fake, tmp_cfg)


class SpecRun:
    """A feature run whose commits are real, in a linked worktree on `flow/feature/crm` (see `spec_run`)."""

    BRANCH = "flow/feature/crm"

    def __init__(self, run, repo: Path, worktree: Path) -> None:
        self.run = run
        self.repo = repo
        self.worktree = worktree

    def revisions(self, path: str) -> list[dict]:
        """`path`'s revisions as `artifacts.run_artifacts` lists them."""
        from agentflow.artifacts import run_artifacts

        return next(a for a in run_artifacts(self.run, self.repo) if a["path"] == path)["revisions"]

    def head(self) -> str:
        return run_git(self.worktree, "rev-parse", "HEAD").strip()

    def commit(self, key: str, write: dict[str, str] | None = None, link: dict[str, str] | None = None,
               delete: list[str] | None = None) -> str:
        """Change the worktree (`write` {path: text}, `link` {path: symlink target}, `delete` [path]), commit it,
        emit `step_committed` for `key` with the new sha, and return that sha."""
        for rel, text in (write or {}).items():
            target = self.worktree / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                target.unlink()
            target.write_text(text, encoding="utf-8")
        for rel, dest in (link or {}).items():
            target = self.worktree / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(dest)
        for rel in delete or []:
            (self.worktree / rel).unlink()
        run_git(self.worktree, "add", "-A")
        run_git(self.worktree, "commit", "-q", "-m", key)
        sha = self.head()
        self.run.emit("step_committed", key=key, sha=sha, summary=key)
        return sha


@pytest.fixture
def spec_run(flow_repo: FlowRepo, tmp_path: Path) -> SpecRun:
    """A feature run in a linked worktree on branch flow/feature/crm. define committed docs/specs/x.md three times:
    define/draft ("Scope 3 says one thing", plus notes.md), define/spec_reconcile after a phase_rejected round 1
    ("Scope 3 says another"), and define/spec_reconcile again after round 2 (another line added, the Scope line kept).
    Then phase_approved define, phase_started plan, and plan/plan committed docs/plans/x.md."""
    run = flow_repo.new_run()
    worktree = tmp_path / "wt"
    run_git(flow_repo.root, "worktree", "add", "-q", "-b", SpecRun.BRANCH, str(worktree))
    spec = SpecRun(run, flow_repo.root, worktree)
    base = spec.head()
    run.emit("run_updated", branch=SpecRun.BRANCH, worktree=str(worktree), base_sha=base)
    run.emit("phase_started", phase="define", start_sha=base)
    spec.commit("define/draft", write={"docs/specs/x.md": "# X\n\nScope 3 says one thing\n", "notes.md": "notes\n"})
    run.emit("phase_rejected", phase="define", round=1, note="say another thing")
    spec.commit("define/spec_reconcile", write={"docs/specs/x.md": "# X\n\nScope 3 says another\n"})
    run.emit("phase_rejected", phase="define", round=2, note="add a line")
    spec.commit("define/spec_reconcile",
                write={"docs/specs/x.md": "# X\n\nScope 3 says another\nScope 4 is new\n"})
    run.emit("phase_approved", phase="define", by="me")
    run.emit("phase_started", phase="plan", start_sha=spec.head())
    spec.commit("plan/plan", write={"docs/plans/x.md": "# Plan\n\n- [ ] Task 1\n"})
    return spec


def awaiting(flow_repo: FlowRepo, module: str = "crm", phase: str = "plan"):
    """A feature run for `module` that raised `phase`'s approval and waits on it."""
    run = flow_repo.new_run({"module": module})
    run.emit("phase_started", phase=phase)
    run.emit("phase_approval_raised", phase=phase, round=0, summary=f"Phase {phase} is done")
    return run


PLAN5 = """# P
### Task 1: One
- [x] done
### Task 2: Two
**Depends on:** Task 1
### Task 3: Three
**Depends on:** Task 2
### Task 4: Four
**Depends on:** Task 2
### Task 5: Five
**Depends on:** Task 1
"""


@pytest.fixture
def building(spec_run: SpecRun) -> SpecRun:
    """spec_run (U1), plus the plan docs/plans/2026-11-16-crm.md, task 1 committed and task 2 blocked."""
    spec_run.commit("plan/plan", write={"docs/plans/2026-11-16-crm.md": PLAN5})
    run, p = spec_run.run, "build/build"
    run.emit("phase_started", phase="build", start_sha=spec_run.head())
    run.emit("step_started", key=f"{p}/task-1/implement")
    run.emit("agent_started", key=f"{p}/task-1/implement", session_id="s1", pid=1, model="sonnet", effort="high")
    run.emit("agent_exited", session_id="s1", exit_code=0, cost=1.25)
    sha = spec_run.commit(f"{p}/task-1", write={"one.py": "x = 1\n"})
    run.emit("task_committed", key=f"{p}/task-1", n=1, title="One", sha=sha, round=0)
    run.emit("step_started", key=f"{p}/task-2/implement")
    run.emit("hitl_raised", kind="blocked", key=f"{p}/task-2/implement/resolve-2", summary="floor_guard fails")
    return spec_run


# ---- the dashboard JS under node: each file runs in a bare context (no DOM), then one expression is read back
NODE = shutil.which("node")
LOADER = ("const fs=require('fs'),vm=require('vm');const ctx=vm.createContext({URLSearchParams});"
          "for (const f of JSON.parse(process.argv[1])) vm.runInContext(fs.readFileSync(f,'utf8'),ctx);"
          "ctx.args=JSON.parse(fs.readFileSync(0,'utf8'));"
          "process.stdout.write(JSON.stringify(vm.runInContext(process.argv[2],ctx)));")


def _js_paths(files: list[str]) -> str:
    """The files to load: a name is read from `dashboard_ui/`, an absolute path (a test stub) as it is."""
    return json.dumps([f if Path(f).is_absolute() else str(ENGINE / "dashboard_ui" / f) for f in files])


def js(files: list[str], expr: str, args) -> object:
    out = subprocess.run([NODE, "-e", LOADER, _js_paths(files), expr],
                         input=json.dumps(args), capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


ASYNC_LOADER = LOADER.replace("process.stdout.write(JSON.stringify(vm.runInContext(process.argv[2],ctx)));",
                              "Promise.resolve(vm.runInContext(process.argv[2],ctx))"
                              ".then((v)=>process.stdout.write(JSON.stringify(v)));")


def js_async(files: list[str], expr: str, args) -> object:
    """`js`, for an expression that returns a promise: what it resolves to."""
    assert ASYNC_LOADER != LOADER
    out = subprocess.run([NODE, "-e", ASYNC_LOADER, _js_paths(files), expr],
                         input=json.dumps(args), capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def walk(nodes):
    for node in nodes:
        yield node
        yield from walk(node.get("c") or [])


def L(obj) -> bytes:
    return json.dumps(obj).encode()


def session_with_transcript(flow_repo, lines=0, width=0, raw=None, tail=b""):
    run, key = flow_repo.new_run(), "build/build/task-1/implement"
    run.emit("agent_started", step=key, pid=None, agent="claude", model="sonnet", effort="high", role="build",
             session_id="x1", sizer=False)
    run.emit("agent_exited", session_id="x1", exit_code=0, cost=0.0, key=key)
    body = b"".join(L({"type": "assistant", "message": {"content": [{"type": "text", "text": "w" * width}]}}) + b"\n"
                    for _ in range(lines))
    body += b"".join(r + b"\n" for r in raw or []) + tail
    path = run.transcript_path(key, 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return run, "s2"


# A Dependabot alert as GitHub's REST API returns it (`GET /repos/{owner}/{repo}/dependabot/alerts/{n}`), cut to the
# fields the vuln lane reads. Tests copy it with `copy.deepcopy` before they change a field.
VULN_ALERT: dict[str, Any] = {
    "number": 42,
    "state": "open",
    "html_url": "https://github.com/acme/app/security/dependabot/42",
    "dependency": {"package": {"ecosystem": "pip", "name": "requests"}, "manifest_path": "requirements.txt"},
    "security_advisory": {
        "ghsa_id": "GHSA-cfgh-2345-vwxj",
        "cve_id": "CVE-2024-1234",
        "summary": "Requests leaks credentials on redirect",
        "description": "A crafted redirect makes requests resend the Authorization header to another host.",
        "severity": "high",
        "identifiers": [{"type": "GHSA", "value": "GHSA-cfgh-2345-vwxj"}, {"type": "CVE", "value": "CVE-2024-1234"}],
        "references": [{"url": "https://github.com/advisories/GHSA-cfgh-2345-vwxj"}],
    },
    "security_vulnerability": {
        "vulnerable_version_range": "< 2.32.0",
        "first_patched_version": {"identifier": "2.32.0"},
        "severity": "high",
    },
}
