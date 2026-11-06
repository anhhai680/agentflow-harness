from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

import pytest
import yaml

from agentflow import pack
from agentflow.config import PROMPT_PLACEHOLDERS, ConfigError, load_config, validate
from agentflow.config_trust import ConfigUntrusted, trusted_config_path
from agentflow.conditions import make_ctx
from agentflow.state import Run, runs_root, workspaces_root
from conftest import PYTHON_CONFIG, PYTHON_SKILLS, deep_merge, run_git


def write_pack(root: Path, patch: dict) -> Path:
    """Commit `<root>/.agentflow/config.yaml`: the `python` pack's config with `patch` deep-merged."""
    data = deep_merge(yaml.safe_load(PYTHON_CONFIG.read_text(encoding="utf-8")), patch)
    target = root / ".agentflow" / "config.yaml"
    target.parent.mkdir(exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "Add the agentflow pack")
    return target


def make_worktree(root: Path, git) -> Path:
    """A linked worktree of `root` on a new branch `other`, beside it."""
    wt = root.parent / f"{root.name}-wt"
    git(root, "worktree", "add", "-q", "-b", "other", str(wt))
    return wt


def test_pack_prompt_wins_over_engine_default(tmp_path):
    (tmp_path / "prompts").mkdir(); (tmp_path / "prompts/step.md").write_text("pack")
    assert pack.prompt_path(tmp_path / "config.yaml", "step.md") == tmp_path / "prompts/step.md"
    assert pack.prompt_path(tmp_path / "config.yaml", "debug.md") == pack.ENGINE_PROMPTS / "debug.md"
    assert pack.prompt_path(tmp_path / "config.yaml", "nope.md") is None


def test_prompt_files_and_labels(tmp_path):
    config = tmp_path / "config.yaml"
    (tmp_path / "prompts").mkdir(); (tmp_path / "prompts/step.md").write_text("pack")
    files = pack.prompt_files(config)
    assert files["step.md"] == tmp_path / "prompts/step.md"
    assert files["debug.md"] == pack.ENGINE_PROMPTS / "debug.md"
    assert pack.prompt_label(config, files["step.md"]) == "prompts/step.md"
    assert pack.prompt_label(config, files["debug.md"]) == "engine:prompts/debug.md"


def test_bad_placeholder_in_a_pack_override_is_reported_against_the_pack_file(tmp_cfg, repo_root):
    path = tmp_cfg()
    (path.parent / "prompts" / "verify.md").write_text("{not_a_placeholder}\n{brief}\n")
    issues = validate(load_config(path), repo_root)
    assert any(i.path == "prompts/verify.md" and "not_a_placeholder" in i.message for i in issues)


def test_override_without_brief_is_reported_even_though_the_engine_default_has_one(tmp_cfg, repo_root):
    path = tmp_cfg()
    (path.parent / "prompts" / "verify.md").write_text("no brief here\n")
    assert any("prompts/verify.md has no {brief}" in i.message for i in validate(load_config(path), repo_root))


def test_step_naming_an_unknown_prompt_is_an_error(tmp_cfg, repo_root):
    def patch(data):
        next(s for s in data["phases"]["verify"]["steps"] if s["id"] == "verify")["prompt"] = "nope.md"

    issues = validate(load_config(tmp_cfg(patch)), repo_root)
    assert any("is not in the pack's prompts/ or the engine's" in i.message for i in issues)


# --------------------------------------------------------------------------- where the pack is, and which it is


def test_pack_config_from_a_linked_worktree_is_the_main_checkouts(tmp_git, git):
    write_pack(tmp_git, {})
    wt = make_worktree(tmp_git, git)
    (wt / ".agentflow" / "config.yaml").write_text("version: 2\n", encoding="utf-8")  # the other branch's copy
    assert pack.pack_config(wt) == (tmp_git / ".agentflow/config.yaml").resolve()
    assert pack.pack_config(wt / "src") == (tmp_git / ".agentflow/config.yaml").resolve()  # from a subdirectory too


def test_missing_pack_names_the_path(tmp_git):
    with pytest.raises(pack.PackError, match=r"no \.agentflow/config\.yaml in "):
        pack.pack_config(tmp_git)


@pytest.mark.parametrize("spec,ok", [(">=2,<3", True), (">=2.1", False), ("==2.0", True), ("<2", False), (">1", True),
                                     ("<=2", True), (">= 2 , < 3", True)])
def test_engine_satisfies(spec, ok):
    assert pack.engine_satisfies(spec, "2.0") is ok


@pytest.mark.parametrize("spec", ["~2", "", "2", ">=", ">=2,", ">=two", "=>2"])
def test_engine_spec_garbage_is_refused(spec):
    with pytest.raises(pack.PackError):
        pack.engine_satisfies(spec)


def test_version_1_pack_is_refused(tmp_cfg):
    with pytest.raises(ConfigError, match="pack format 1; this engine reads format 2"):
        load_config(tmp_cfg({"version": 1}))


def test_engine_range_that_excludes_this_engine_is_refused(tmp_cfg):
    with pytest.raises(ConfigError, match=r"engine: '>=3' .* this engine is 2\.0"):
        load_config(tmp_cfg({"engine": ">=3"}))


def test_engine_range_that_does_not_parse_is_refused(tmp_cfg):
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_cfg({"engine": "~2"}))
    assert exc.value.path == "engine"


def test_pack_without_an_engine_range_is_refused(tmp_cfg):
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_cfg({"engine": None}))
    assert exc.value.path == "engine"


def test_pack_dir_is_forbidden_even_when_the_pack_omits_it(tmp_cfg):
    cfg = load_config(tmp_cfg(lambda d: d["guardrails"].__setitem__("forbidden_paths", ["X.md"])))
    assert cfg.guardrails.forbidden_paths == ["X.md", ".agentflow/**"]


def test_pack_that_names_the_pack_dir_itself_keeps_one_entry(tmp_cfg):
    cfg = load_config(tmp_cfg(lambda d: d["guardrails"].__setitem__("forbidden_paths", [".agentflow/**", "X.md"])))
    assert cfg.guardrails.forbidden_paths == [".agentflow/**", "X.md"]


def test_pack_without_a_verify_gate_set_is_refused(tmp_cfg, repo_root):
    cfg = load_config(tmp_cfg(lambda d: d["gate_sets"].pop("verify")))
    assert any(i.level == "error" and i.path == "gate_sets.verify" for i in validate(cfg, repo_root))


def test_pack_with_an_empty_verify_gate_set_is_refused(tmp_cfg, repo_root):
    cfg = load_config(tmp_cfg(lambda d: d["gate_sets"].__setitem__("verify", [])))
    assert any(i.level == "error" and i.path == "gate_sets.verify" for i in validate(cfg, repo_root))


def test_resuming_a_run_whose_config_is_gone_names_the_pack(pack_repo_python, tmp_path, cfg):
    run = Run.create(cfg, pack_repo_python, "feature", {"module": "greet"})
    gone = tmp_path / "agentflow" / "config.yaml"  # where the engine kept its config before the pack
    run.emit("run_options", config=str(gone), config_sha256="0" * 64)
    with pytest.raises(ConfigUntrusted, match=r"--config \.agentflow/config\.yaml"):
        trusted_config_path(run, None, pack.pack_config(run.repo))


def test_python_pack_validates_clean(pack_repo_python):
    assert validate(load_config(pack.pack_config(pack_repo_python)), pack_repo_python) == []


def test_cli_inside_a_linked_worktree_reads_the_main_checkouts_pack(pack_repo_python, git, monkeypatch, capsys):
    from agentflow import flow

    wt = make_worktree(pack_repo_python, git)
    (wt / ".agentflow" / "config.yaml").write_text("version: 1\n", encoding="utf-8")  # the worktree's copy: unusable
    monkeypatch.chdir(wt)
    assert flow.main(["validate"]) == 0, capsys.readouterr().out
    assert "ok: config.yaml is valid" in capsys.readouterr().out


def test_cli_without_a_pack_says_so(tmp_git, monkeypatch, capsys):
    from agentflow import flow

    monkeypatch.chdir(tmp_git)
    assert flow.main(["validate"]) == 1
    assert "no .agentflow/config.yaml in " in capsys.readouterr().out
    assert flow.main(["explain", "feature", "--module", "greet"]) == 1
    assert "this repository has no agentflow pack" in capsys.readouterr().out


def test_engine_default_prompts_use_only_known_placeholders():
    for file in pack.ENGINE_PROMPTS.glob("*.md"):
        names = set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", file.read_text(encoding="utf-8")))
        assert names <= PROMPT_PLACEHOLDERS, file.name


# --------------------------------------------------------------------------- paths from the pack


def copy_skills(root: Path, into: str) -> Path:
    """The `python` pack's skill stubs copied to `<root>/<into>/`; returns `root`."""
    shutil.copytree(PYTHON_SKILLS, root / into)
    return root


def test_state_dir_relative_value_is_relative_to_the_main_checkout(tmp_git, git):
    write_pack(tmp_git, {"paths": {"state_dir": "var/af"}})
    wt = make_worktree(tmp_git, git)
    assert pack.state_dir(wt) == (tmp_git / "var/af").resolve()


def test_state_dir_expands_the_home_dir(tmp_git, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    write_pack(tmp_git, {"paths": {"state_dir": "~/af"}})
    assert pack.state_dir(tmp_git) == (tmp_path / "home" / "af").resolve()


def test_state_dir_default_is_outside_the_repo(tmp_git, monkeypatch, tmp_path):
    write_pack(tmp_git, {}); monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    got = pack.state_dir(tmp_git)
    assert got.parent == tmp_path / "xdg/agentflow" and got.name.startswith(tmp_git.name + "-")


def test_state_dir_default_is_resolved_through_a_symlinked_xdg_home(tmp_git, monkeypatch, tmp_path):
    real = tmp_path / "real-xdg"
    real.mkdir()
    (tmp_path / "xdg-link").symlink_to(real, target_is_directory=True)
    write_pack(tmp_git, {}); monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-link"))
    got = pack.state_dir(tmp_git)
    assert got == got.resolve() and got.parent == real.resolve() / "agentflow"


def test_state_dir_default_is_the_same_from_a_worktree_and_without_a_pack(tmp_git, git):
    wt = make_worktree(tmp_git, git)
    assert pack.state_dir(wt) == pack.state_dir(tmp_git)
    digest = hashlib.sha256(str(tmp_git.resolve()).encode()).hexdigest()[:8]
    assert pack.state_dir(tmp_git).name == f"{tmp_git.name}-{digest}"


def test_state_dir_follows_a_rewritten_pack(tmp_git):
    write_pack(tmp_git, {"paths": {"state_dir": "one"}})
    assert pack.state_dir(tmp_git) == (tmp_git / "one").resolve()
    write_pack(tmp_git, {"paths": {"state_dir": "second"}})  # another size too: the cache keys on mtime and size
    assert pack.state_dir(tmp_git) == (tmp_git / "second").resolve()


def test_state_dir_of_a_directory_outside_git_is_its_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    plain = tmp_path / "plain"
    plain.mkdir()
    assert pack.state_dir(plain).parent == tmp_path / "xdg/agentflow"


def test_runs_and_workspaces_live_in_the_state_dir(tmp_git):
    write_pack(tmp_git, {"paths": {"state_dir": "var/af"}})
    assert runs_root(tmp_git) == (tmp_git / "var/af/runs").resolve()
    assert workspaces_root(tmp_git) == (tmp_git / "var/af/workspaces").resolve()


def test_state_dir_inside_the_repo_and_not_ignored_warns(tmp_git):
    cfg_with_state_dir = load_config(write_pack(tmp_git, {"paths": {"state_dir": "state"}}))
    assert any(i.level == "warning" and i.path == "paths.state_dir" for i in validate(cfg_with_state_dir, tmp_git))


def test_state_dir_inside_the_repo_and_ignored_does_not_warn(tmp_git):
    (tmp_git / ".gitignore").write_text("/state/\n", encoding="utf-8")
    cfg = load_config(write_pack(tmp_git, {"paths": {"state_dir": "state"}}))
    assert not [i for i in validate(cfg, tmp_git) if i.path == "paths.state_dir"]


def test_state_dir_outside_the_repo_does_not_warn(tmp_git, tmp_path):
    cfg = load_config(write_pack(tmp_git, {"paths": {"state_dir": str(tmp_path / "elsewhere")}}))
    assert not [i for i in validate(cfg, tmp_git) if i.path == "paths.state_dir"]


def test_state_dir_that_is_not_a_string_is_an_error(tmp_cfg, repo_root):
    cfg = load_config(tmp_cfg({"paths": {"state_dir": 3}}))
    assert any(i.level == "error" and i.path == "paths.state_dir" for i in validate(cfg, repo_root))


def test_skills_dir_defaults_to_claude_skills(tmp_cfg):
    assert load_config(tmp_cfg()).project.skills_dir == ".claude/skills"


def test_skills_dir_is_read_from_the_pack(tmp_cfg, tmp_path):
    repo = copy_skills(tmp_path, into="skills")                     # the python pack's stubs copied to skills/
    cfg = load_config(tmp_cfg({"project": {"skills_dir": "skills"}}))
    (tmp_path / ".claude" / "skills").unlink()                      # only the pack's own skills dir holds them
    assert not [i for i in validate(cfg, repo) if i.message.startswith("skill ")]  # (briefs name .claude/skills)
    missing = load_config(tmp_cfg({"project": {"skills_dir": "nowhere"}}))
    assert any("nowhere/test-driven-development/SKILL.md" in i.message for i in validate(missing, repo))


def test_skills_dir_is_normalized(tmp_cfg):
    assert load_config(tmp_cfg({"project": {"skills_dir": "./skills/"}})).project.skills_dir == "skills"
    assert load_config(tmp_cfg({"project": {"skills_dir": "a/./b//"}})).project.skills_dir == "a/b"


@pytest.mark.parametrize("skills_dir", ["/abs/skills", "../skills", "skills/../../x", ".", ""])
def test_skills_dir_outside_the_repo_or_not_a_subdir_is_an_error(tmp_cfg, repo_root, skills_dir):
    cfg = load_config(tmp_cfg({"project": {"skills_dir": skills_dir}}))
    assert any(i.level == "error" and i.path == "project.skills_dir" for i in validate(cfg, repo_root))


def test_read_file_skill_lines_name_the_skills_dir():
    from agentflow.lanes import _read_file_skills

    text = _read_file_skills("Use the `writing-plans` skill.", "skills")
    assert "`skills/writing-plans/SKILL.md`" in text


def test_spec_placeholder_uses_spec_file(tmp_cfg, repo_root):
    cfg = load_config(tmp_cfg({"project": {"docs": {"spec_file": "{module}-spec.md"}}}))
    assert make_ctx(cfg, repo_root, "feature", {"module": "crm"}).values["spec"] == "docs/specs/crm-spec.md"


def test_spec_file_defaults_to_the_module_name(tmp_cfg, repo_root):
    cfg = load_config(tmp_cfg({"project": {"docs": {"spec_file": None}}}))
    assert cfg.project.spec_file == "{module}.md"
    assert make_ctx(cfg, repo_root, "feature", {"module": "crm"}).values["spec"] == "docs/specs/crm.md"


def test_idea_placeholder_is_empty_without_an_idea_doc(tmp_cfg, repo_root):
    cfg = load_config(tmp_cfg({"project": {"docs": {"idea": None}}}))
    assert make_ctx(cfg, repo_root, "feature", {"module": "crm"}).values["idea"] == ""


@pytest.mark.parametrize("key", ["specs", "plans"])
def test_docs_specs_and_plans_are_required(tmp_cfg, repo_root, key):
    cfg = load_config(tmp_cfg(lambda data: data["project"]["docs"].__setitem__(key, None)))  # `null`: no default either
    assert any(i.level == "error" and i.path == f"project.docs.{key}" for i in validate(cfg, repo_root))


@pytest.mark.parametrize("key", ["idea", "decisions"])
def test_doc_checks_need_the_idea_doc_and_the_decisions_dir(tmp_cfg, repo_root, key):
    cfg = load_config(tmp_cfg({"project": {"docs": {key: None}}}))
    errors = [i for i in validate(cfg, repo_root) if i.level == "error"]
    assert errors and all(i.path.endswith(".checks") and f"project.docs.{key}" in i.message for i in errors)


# --------------------------------------------------------------------------- bundled skills

def _skill(root: Path, skills_dir: str, name: str, text: str = "repo") -> Path:
    path = root / skills_dir / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_skill_file_prefers_the_repos_skill(tmp_path):
    own = _skill(tmp_path, ".claude/skills", "agentflow-plan")
    assert pack.skill_file(tmp_path, ".claude/skills", "agentflow-plan") == own
    assert not pack.is_bundled(own)


def test_skill_file_falls_back_to_the_bundled_skill(tmp_path):
    found = pack.skill_file(tmp_path, ".claude/skills", "agentflow-plan")
    assert found == pack.ENGINE_SKILLS / "agentflow-plan" / "SKILL.md" and found.is_file()
    assert pack.is_bundled(found)
    assert pack.skill_file(tmp_path, ".claude/skills", "no-such-skill") is None


def test_skill_file_follows_a_symlinked_skills_dir(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    own = _skill(elsewhere, "skills", "mine")
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "skills").symlink_to(elsewhere / "skills", target_is_directory=True)  # like the fixtures
    found = pack.skill_file(repo, ".claude/skills", "mine")
    assert found is not None and found.read_text(encoding="utf-8") == "repo" and found.samefile(own)


@pytest.mark.parametrize("name", ["../agentflow-plan", "a/b", "", ".", "..", "x\\y", "-x", "a\0b", "a b", None, 3])
def test_skill_file_refuses_a_name_that_is_not_a_skill_name(tmp_path, name):
    _skill(tmp_path, ".claude/skills", "x")                       # a name must not reach anything outside a skill dir
    (tmp_path / "SKILL.md").write_text("outside", encoding="utf-8")
    assert pack.skill_file(tmp_path, ".claude/skills", name) is None


@pytest.mark.parametrize("skills_dir", ["/abs", "../out", "a/../../b", "..", "", "a\\..\\b", None, 3])
def test_skill_file_refuses_a_skills_dir_outside_the_repo(tmp_path, skills_dir):
    _skill(tmp_path.parent / f"{tmp_path.name}-out", "skills", "agentflow-plan")
    assert pack.skill_file(tmp_path, skills_dir, "agentflow-plan") is None


def test_skill_file_does_not_let_a_symlinked_skill_dir_name_escape_the_rule(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "SKILL.md").write_text("x", encoding="utf-8")
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "linked").symlink_to(tmp_path / "out", target_is_directory=True)
    assert pack.skill_file(tmp_path, "skills", "linked") == tmp_path / "skills" / "linked" / "SKILL.md"  # the name is the rule


BUNDLED = sorted(p for p in pack.ENGINE_SKILLS.iterdir() if p.is_dir())
STAGES = ("define", "plan", "build", "debug", "verify", "review", "respond")


def test_the_bundle_holds_one_skill_per_stage():
    assert [p.name for p in BUNDLED] == sorted(f"agentflow-{s}" for s in STAGES)


def test_every_bundled_skill_names_itself_in_its_frontmatter():
    for directory in BUNDLED:
        lines = (directory / "SKILL.md").read_text(encoding="utf-8").splitlines()
        assert len(lines) <= 80, directory.name
        assert lines[0] == "---" and lines[1] == f"name: {directory.name}", directory.name
        end = lines.index("---", 1)
        assert any(line.startswith("description: ") and line[13:].strip() for line in lines[2:end]), directory.name


def _bundled_text(stage: str) -> str:
    return (pack.ENGINE_SKILLS / f"agentflow-{stage}" / "SKILL.md").read_text(encoding="utf-8")


def test_the_bundled_review_skill_uses_the_prompts_vocabulary():
    text = _bundled_text("review")
    for word in ("Critical", "Important", "Minor", "Declined to judge", "Output Format"):
        assert word in text
    for prompt in ("branch_review.md", "task_review.md"):  # the words the prompts map to blocking and minor
        prompts = (pack.ENGINE_PROMPTS / prompt).read_text(encoding="utf-8")
        assert "Critical or Important" in prompts and "Minor" in prompts and "Declined to judge" in prompts


def test_bundled_build_and_verify_skills_leave_the_gates_to_the_runner():
    build, verify = _bundled_text("build").lower(), _bundled_text("verify").lower()
    assert "never commit" in build and "never run the whole suite" in build and "gates" in build
    assert "do not run" in verify and "again" in verify and "evidence" in verify


def test_the_bundled_plan_skill_states_the_format_the_engine_reads():
    from agentflow import sources
    text = _bundled_text("plan")
    for part in ("### Task N:", "**Depends on:**", "**Files:**", "- Create:", "- Modify:", "- Test:", "- [ ] **Step"):
        assert part in text
    assert str(sources.MAX_TASK_STEPS) in text and str(sources.MAX_TASK_FILES) in text


def test_a_plan_written_as_the_bundled_skill_says_parses(tmp_path):
    from agentflow import sources
    plan = tmp_path / "plan.md"
    plan.write_text(
        "# Plan\n\n### Task 1: First\n\n**Depends on:** none\n\n**Files:**\n- Create: `a.py`\n- Modify: `b.py`\n"
        "- Test: `t.py`\n\n- [ ] **Step 1: Write the failing test**\n- [ ] **Step 2: Make it pass**\n\n"
        "### Task 2: Second\n\n**Depends on:** Task 1\n\n**Files:**\n- Modify: `a.py`\n\n- [ ] **Step 1: Do it**\n",
        encoding="utf-8")
    first, second = sources.parse_plan(plan)
    assert (first.n, first.title, first.depends_on, sources.task_size(first)) == (1, "First", [], (2, 3))
    assert (second.n, second.depends_on, sources.task_size(second)) == (2, [1], (1, 1))
