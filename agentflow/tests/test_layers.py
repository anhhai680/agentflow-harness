"""`layers`: the merge of the config layers, `null` deletion, phase knobs, and the user file's personal-only rule."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from agentflow import layers
from agentflow.layers import Layer, LayerError, check_user, merge, origin_of, owns_workflow

USER_FILE = Path("/home/dev/.config/agentflow/config.yaml")


def L(name: str, data: dict) -> Layer:
    return Layer(name, data, None)


def workflow() -> dict:
    return {
        "lanes": {"feature": {"phases": ["build", "verify"]}},
        "phases": {"build": {"steps": [{"skill": "x"}], "model": "m"}, "verify": {"steps": [{"gate": "g"}]}},
    }


def pack_with_agents() -> dict:
    return {"agents": {"claude": {"command": ["claude"], "models": {"build": "opus"}}}}


def test_mappings_merge_and_lists_replace():
    low = L("engine", {"defaults": {"a": 1, "b": 2}, "list": [1, 2], "x": {"y": 1}, "t": 1})
    high = L("project", {"defaults": {"b": 3, "c": 4}, "list": [9], "x": 5, "t": {"k": 1}})
    merged, origins = merge([low, high])
    assert merged == {"defaults": {"a": 1, "b": 3, "c": 4}, "list": [9], "x": 5, "t": {"k": 1}}
    assert origins[("defaults", "a")] == "engine"
    assert origins[("defaults", "b")] == "project"
    assert origins[("defaults", "c")] == "project"
    assert origins[("list",)] == "project"
    assert origins[("x",)] == "project" and ("x", "y") not in origins  # the mismatched type replaced the mapping
    assert origins[("t", "k")] == "project" and ("t",) not in origins


def test_empty_containers_are_values_and_empty_mapping_is_a_leaf():
    low = L("engine", {"a": {"k": 1}, "b": [1], "c": 1})
    high = L("project", {"a": {}, "b": [], "c": {}, "d": {}})
    merged, origins = merge([low, high])
    assert merged == {"a": {"k": 1}, "b": [], "c": {}, "d": {}}  # merging {} into a mapping changes nothing
    assert origins[("a", "k")] == "engine"
    assert origins[("b",)] == "project" and origins[("c",)] == "project" and origins[("d",)] == "project"


def test_null_deletes_a_lower_key():
    low = L("engine", {"agents": {"claude": {"a": 1}, "codex": {"b": 2}}, "gates": {"g": {"run": "x"}}, "keep": 1})
    high = L("project", {"agents": {"codex": None}, "gates": None, "never": None})
    merged, origins = merge([low, high])
    assert merged == {"agents": {"claude": {"a": 1}}, "keep": 1}
    assert not any(k[0] == "gates" or k[:2] == ("agents", "codex") for k in origins)
    assert origins[("agents", "claude", "a")] == "engine"
    # a deleted key can be set again by a still higher layer
    merged, origins = merge([low, high, L("user", {"gates": {"g": {"run": "y"}}})])
    assert merged["gates"] == {"g": {"run": "y"}} and origins[("gates", "g")] == "user"


def test_merge_never_mutates_or_shares_its_inputs():
    low_data = {"a": {"list": [1, {"n": 1}], "m": {"deep": {"x": 1}}}, "gates": {"g": {"run": ["a"]}}}
    high_data = {"a": {"list2": [3], "m": {"deep": {"y": 2}}}, "gates": {"h": {"run": ["b"], "env": {"k": "v"}}},
                 "record_step": {"skill": "s", "args": [1]}, "e": {}}
    low, high = L("engine", low_data), L("project", high_data)
    low_before, high_before = copy.deepcopy(low_data), copy.deepcopy(high_data)
    merged, _ = merge([low, high])
    assert low_data == low_before and high_data == high_before
    assert merged["a"]["list"] is not low_data["a"]["list"]
    assert merged["a"]["list"][1] is not low_data["a"]["list"][1]
    assert merged["a"]["m"]["deep"] is not low_data["a"]["m"]["deep"]
    assert merged["a"]["list2"] is not high_data["a"]["list2"]
    assert merged["gates"]["g"] is not low_data["gates"]["g"]
    assert merged["gates"]["g"]["run"] is not low_data["gates"]["g"]["run"]
    assert merged["gates"]["h"]["env"] is not high_data["gates"]["h"]["env"]
    assert merged["record_step"] is not high_data["record_step"]
    assert merged["record_step"]["args"] is not high_data["record_step"]["args"]
    assert merged["e"] is not high_data["e"]
    merged["a"]["list"].append(99)  # changing the result later leaves the inputs alone
    merged["gates"]["g"]["run"].append("z")
    assert low_data == low_before and high_data == high_before
    # a single layer's data isn't shared either
    solo, _ = merge([low])
    assert solo == low_before and solo["a"] is not low_data["a"]


def test_a_named_condition_and_a_gate_are_replaced_whole():
    low = L("engine", {
        "conditions": {"c": {"all": ["a"], "extra": 1}, "keep": {"x": 1}},
        "gates": {"g": {"run": "old", "timeout": 5}, "keep": {"run": "k"}},
        "record_step": {"skill": "old", "model": "m"},
        "resolve_step": {"skill": "r", "model": "m"},
    })
    high = L("project", {
        "conditions": {"c": {"any": ["b"]}},
        "gates": {"g": {"run": "new"}},
        "record_step": {"skill": "new"},
        "resolve_step": {},
    })
    merged, origins = merge([low, high])
    assert merged["conditions"] == {"c": {"any": ["b"]}, "keep": {"x": 1}}
    assert merged["gates"] == {"g": {"run": "new"}, "keep": {"run": "k"}}
    assert merged["record_step"] == {"skill": "new"}
    assert merged["resolve_step"] == {}
    assert origins[("conditions", "c")] == "project" and ("conditions", "c", "all") not in origins
    assert origins[("gates", "g")] == "project" and ("gates", "g", "timeout") not in origins
    assert origins[("conditions", "keep")] == "engine"  # a whole entry is the leaf
    assert origins[("record_step",)] == "project" and origins[("resolve_step",)] == "project"


def test_a_layer_with_steps_drops_the_lower_workflow():
    low = L("engine", {**workflow(), "ci": {"wait": True}, "skills": {"map": {"a": "b"}}, "autonomy": {"x": 1},
                       "conditions": {"c": {"all": []}}, "record_step": {"skill": "r"}, "defaults": {"k": 1}})
    high = L("project", {
        "lanes": {"bug": {"phases": ["fix"]}},
        "phases": {"fix": {"steps": [{"skill": "f"}]}},
        "ci": {"timeout": 3},
    })
    merged, origins = merge([low, high])
    assert merged["lanes"] == {"bug": {"phases": ["fix"]}}
    assert merged["phases"] == {"fix": {"steps": [{"skill": "f"}]}}
    assert merged["ci"] == {"timeout": 3}  # the lower `ci` was dropped, not merged
    for gone in ("conditions", "record_step", "autonomy", "skills"):
        assert gone not in merged and not any(k[0] == gone for k in origins)
    assert merged["defaults"] == {"k": 1} and origins[("defaults", "k")] == "engine"  # not a workflow key
    assert origins[("lanes", "bug", "phases")] == "project"
    assert not any(k[:2] == ("phases", "build") for k in origins)


def test_a_layer_with_steps_and_no_lanes_is_an_error():
    low = L("engine", workflow())
    with pytest.raises(LayerError) as err:
        merge([low, L("project", {"phases": {"fix": {"steps": []}}})])
    assert err.value.path == "lanes"
    assert "also writes `lanes`" in err.value.message
    assert owns_workflow({"phases": {"fix": {"steps": []}}})
    assert not owns_workflow({"phases": {"build": {"model": "x"}}})
    assert not owns_workflow({"phases": "x"}) and not owns_workflow({"phases": {"a": None}}) and not owns_workflow({})


def test_a_phase_knob_merges_into_the_lower_workflow():
    merged, origins = merge([L("engine", workflow()), L("project", {"phases": {"build": {"needs_approval": True}}})])
    assert merged["phases"]["build"] == {"steps": [{"skill": "x"}], "model": "m", "needs_approval": True}
    assert origins[("phases", "build", "needs_approval")] == "project"
    assert origins[("phases", "build", "model")] == "engine"
    # a knob can be removed with null, and an empty `phases` changes nothing
    merged, _ = merge([L("engine", workflow()), L("project", {"phases": {"build": {"model": None}}})])
    assert "model" not in merged["phases"]["build"]
    merged, _ = merge([L("engine", workflow()), L("project", {"phases": {}})])
    assert merged["phases"] == workflow()["phases"]


@pytest.mark.parametrize("phases", [None, [], "x", {"build": None}, {"build": 3}, {"deploy": {"model": "x"}},
                                    {"build": {"skip_if": "x"}}])
def test_a_phase_adjustment_that_is_not_a_knob_is_an_error(phases):
    with pytest.raises(LayerError) as err:
        merge([L("engine", workflow()), L("project", {"phases": phases})])
    expected = {
        "phases": "phases", "x": "phases", "deploy": "phases.deploy", "skip_if": "phases.build.skip_if",
    }
    if phases is None or isinstance(phases, (list, str)):
        assert err.value.path == "phases"
    elif "deploy" in phases:
        assert err.value.path == "phases.deploy"
    elif phases["build"] is None or phases["build"] == 3:
        assert err.value.path == "phases.build"
    else:
        assert err.value.path == expected["skip_if"]
    assert "whole workflow" in err.value.message


def test_a_phase_adjustment_with_no_workflow_below_is_an_error():
    with pytest.raises(LayerError) as err:
        merge([L("project", {"phases": {"build": {"model": "x"}}})])
    assert err.value.path == "phases.build"


def test_the_user_layer_overrides_the_pack_for_personal_keys():
    lower = {**pack_with_agents(), "defaults": {"run_budget_usd": 5, "task_budget_usd": 1},
             "hitl": {"notify": "x", "remind_after_h": 2}, "dashboard": {"port": 1, "host": "127.0.0.1"}}
    user = {"agents": {"claude": {"models": {"build": "sonnet"}}}, "defaults": {"run_budget_usd": 9},
            "hitl": {"notify": "y", "remind_after_h": 4}, "dashboard": {"port": 8000}}
    check_user(user, lower, USER_FILE)
    merged, origins = merge([L("project", lower), L("user", user)])
    assert merged["agents"]["claude"]["models"] == {"build": "sonnet"}
    assert merged["agents"]["claude"]["command"] == ["claude"]
    assert merged["defaults"] == {"run_budget_usd": 9, "task_budget_usd": 1}
    assert merged["dashboard"] == {"port": 8000, "host": "127.0.0.1"}
    assert origins[("dashboard", "port")] == "user" and origins[("dashboard", "host")] == "project"
    assert origins[("agents", "claude", "command")] == "project"
    check_user({}, lower, USER_FILE)  # an empty file is fine


def test_the_hard_budget_is_a_personal_setting():
    assert "defaults.run_budget_hard_usd" in layers.USER_KEYS
    user = {"defaults": {"run_budget_hard_usd": 40}}
    check_user(user, {"defaults": {"run_budget_usd": 5}}, USER_FILE)
    merged, origins = merge([L("project", {"defaults": {"run_budget_usd": 5}}), L("user", user)])
    assert merged["defaults"] == {"run_budget_usd": 5, "run_budget_hard_usd": 40}
    assert origins[("defaults", "run_budget_hard_usd")] == "user"


def test_a_user_model_for_one_role_is_personal():
    check_user({"agents": {"claude": {"models": {"build": "sonnet"}}}}, pack_with_agents(), USER_FILE)
    merged, origins = merge([L("project", pack_with_agents()),
                             L("user", {"agents": {"claude": {"models": {"build": "sonnet"}}}})])
    assert merged["agents"]["claude"]["models"] == {"build": "sonnet"}
    assert origins[("agents", "claude", "models", "build")] == "user"


def nested(dotted: str, value="x") -> dict:
    out: dict = {}
    node = out
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    return out


@pytest.mark.parametrize("key", ["gates.x.run", "agents.claude.command", "guardrails.forbidden_paths",
                                 "phases.build.needs_approval", "dashboard", "policy.enforce",
                                 "policy.obligations.verified"])
def test_the_user_layer_refuses_anything_not_personal(key):
    lower = {**pack_with_agents(), "dashboard": {"port": 1}}
    with pytest.raises(LayerError) as err:
        check_user(nested(key), lower, USER_FILE)
    assert err.value.path == f"{USER_FILE}: {key}"
    assert err.value.message == "not a personal setting; set it in the pack"
    assert str(USER_FILE) in str(err.value) and key in str(err.value)


def test_the_user_layer_refuses_a_command_alongside_a_personal_key():
    user = {"agents": {"claude": {"models": {"build": "x"}, "command": ["evil"]}}}
    with pytest.raises(LayerError) as err:
        check_user(user, pack_with_agents(), USER_FILE)
    assert err.value.path.endswith("agents.claude.command")
    with pytest.raises(LayerError):
        check_user({"gates": None}, pack_with_agents(), USER_FILE)  # a null is a key too
    with pytest.raises(LayerError):
        check_user({"dashboard": {}}, {"dashboard": {"port": 1}}, USER_FILE)  # an empty mapping is not `dashboard.port`


def test_the_user_layer_cannot_create_an_agent():
    with pytest.raises(LayerError) as err:
        check_user({"agents": {"codex": {"models": {"build": "x"}}}}, pack_with_agents(), USER_FILE)
    assert err.value.path == f"{USER_FILE}: agents.codex.models.build"
    assert err.value.message == "no agent `codex` in the pack"
    with pytest.raises(LayerError):
        check_user({"agents": {"claude": {"models": {}}}}, {}, USER_FILE)  # no `agents` at all below


def test_origin_of_finds_the_layer_for_a_dotted_error_path():
    origins = {("gates", "g"): "project", ("defaults", "a"): "engine", ("phases", "build", "model"): "user",
               ("phases",): "starter:go"}
    assert origin_of(origins, "gates.g") == "project"
    assert origin_of(origins, "gates.g.run") == "project"  # below a whole entry
    assert origin_of(origins, "defaults.a") == "engine"
    assert origin_of(origins, "phases.build.model.x") == "user"  # the longest leaf wins over `phases`
    assert origin_of(origins, "phases.verify") == "starter:go"
    assert origin_of(origins, "gates") is None and origin_of(origins, "nope.x") is None
    assert origin_of({}, "a") is None
    assert origin_of(origins, "gatesx") is None  # segments, not characters


def test_user_config_path_follows_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert layers.user_config_path() == tmp_path / "cfg" / "agentflow" / "config.yaml"
    for bad in ("relative/dir", ""):  # a relative or empty value falls back
        monkeypatch.setenv("XDG_CONFIG_HOME", bad)
        assert layers.user_config_path() == tmp_path / "home" / ".config" / "agentflow" / "config.yaml"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert layers.user_config_path() == tmp_path / "home" / ".config" / "agentflow" / "config.yaml"


def test_the_conftest_keeps_the_user_file_inside_the_test_tmp_dir(tmp_path):
    assert layers.user_config_path().is_relative_to(tmp_path)


def test_engine_defaults_path_and_the_exported_keys():
    assert layers.ENGINE_DEFAULTS.parts[-3:] == ("agentflow", "defaults", "config.yaml")
    assert layers.PHASE_FREE_KEYS == ("needs_approval", "resolve", "model", "agent")
    assert "phases" in layers.WORKFLOW_KEYS and "gates" not in layers.WORKFLOW_KEYS
    from agentflow import config_edit
    assert config_edit.PHASE_FREE_KEYS is layers.PHASE_FREE_KEYS


@pytest.mark.parametrize("user", [
    {"agents": {1: {"models": {"build": "x"}}}},          # merge would use the int key itself
    {"agents": {"claude": {"models": {True: "x"}}}},      # YAML 1.1 reads `on:`/`yes:` as a bool
    {None: {"port": 1}},
])
def test_the_user_layer_refuses_a_key_that_is_not_text(user):
    lower = {"agents": {"1": {"command": ["x"]}, "claude": {"command": ["claude"]}}}
    with pytest.raises(LayerError) as err:
        check_user(user, lower, USER_FILE)
    assert err.value.path.startswith(f"{USER_FILE}") and "text" in err.value.message


@pytest.mark.parametrize("data", [["dashboard"], "port: 1", 7])
def test_the_user_layer_refuses_a_file_that_is_not_a_mapping(data):
    with pytest.raises(LayerError) as err:
        check_user(data, pack_with_agents(), USER_FILE)
    assert err.value.path == str(USER_FILE) and "mapping of personal settings" in err.value.message


def test_rows_list_every_leaf_with_its_layer():
    engine = yaml.safe_load(layers.ENGINE_DEFAULTS.read_text(encoding="utf-8"))
    project = {"gates": {"unit": {"run": "true"}}, "hitl": {"remind_after_h": 2},
               "agents": {"claude": {"models": {"build": "opus"}}}}
    user = {"defaults": {"run_budget_usd": 9}}
    merged, origins = merge([L("engine", engine), L("project", project), L("user", user)])
    rows = layers.rows(merged, origins)
    assert ("defaults.step_timeout_min", 45, "engine") in rows
    assert ("defaults.run_budget_usd", 9, "user") in rows
    assert ("gates.unit", {"run": "true"}, "project") in rows                 # a whole entry is one row
    assert ("agents.claude.models.build", "opus", "project") in rows
    assert ("agents.claude.models.plan", "opus", "engine") in rows
    assert ("hitl.remind_after_h", 2, "project") in rows                      # below an engine `{}`
    assert ("record_step", merged["record_step"], "engine") in rows
    assert ("guardrails.forbidden_paths", [], "engine") in rows               # an empty list is a leaf
    keys = [key for key, _, _ in rows]
    assert len(keys) == len(set(keys)) == len(layers.leaf_keys(merged))
    assert keys.index("project.base_branch") < keys.index("gates.unit") < keys.index("defaults.agent")  # merged order
    assert layers.rows({}, {}) == []


# --------------------------------------------------------------------------- starters

STARTER_NAMES = ["docs-only", "go", "node-pnpm", "python-uv"]


def test_starter_names_are_the_sorted_directories_of_the_starters_folder():
    assert layers.STARTERS == Path(layers.__file__).resolve().parent / "starters"
    assert layers.starter_names() == STARTER_NAMES
    for name in STARTER_NAMES:
        assert (layers.STARTERS / name / "config.yaml").is_file()


def test_base_layers_is_the_engine_alone_without_a_starter():
    for absent in (None,):
        stack = layers.base_layers(absent)
        assert [layer.name for layer in stack] == ["engine"] and stack[0].path == layers.ENGINE_DEFAULTS


def test_base_layers_puts_the_named_starter_over_the_engine():
    stack = layers.base_layers("go")
    assert [layer.name for layer in stack] == ["engine", "starter:go"]
    assert stack[1].path == layers.STARTERS / "go" / "config.yaml"
    assert stack[1].data["gates"]["vet"] == {"run": "go vet ./..."}


@pytest.mark.parametrize("value", ["nope", "", "../defaults", "/etc", "go/..", ".", "GO", "go ", 3, True, ["go"], {"go": 1}])
def test_base_layers_refuses_anything_that_is_not_a_listed_name(value):
    with pytest.raises(LayerError) as err:
        layers.base_layers(value)
    assert err.value.path == "starter"
    assert all(name in err.value.message for name in STARTER_NAMES)


def test_base_layers_builds_its_path_from_the_listed_entry_only(monkeypatch, tmp_path):
    (tmp_path / "ok").mkdir()
    (tmp_path / "ok" / "config.yaml").write_text("gates: {}\n", encoding="utf-8")
    (tmp_path / "secret.yaml").write_text("gates: {x: {run: evil}}\n", encoding="utf-8")
    monkeypatch.setattr(layers, "STARTERS", tmp_path)
    assert layers.starter_names() == ["ok"]  # a file in the folder is not a starter
    for sneaky in ("../" + tmp_path.name, "ok/..", "secret.yaml", str(tmp_path / "ok")):
        with pytest.raises(LayerError):
            layers.base_layers(sneaky)
    assert layers.base_layers("ok")[1].path == tmp_path / "ok" / "config.yaml"


def test_starter_names_skip_hidden_and_symlinked_directories(monkeypatch, tmp_path):
    starters, outside = tmp_path / "starters", tmp_path / "outside"
    for folder in (starters / "ok", starters / ".hidden", outside):
        folder.mkdir(parents=True)
        (folder / "config.yaml").write_text("gates: {}\n", encoding="utf-8")
    (starters / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(layers, "STARTERS", starters)
    assert layers.starter_names() == ["ok"]
    for name in (".hidden", "linked"):
        with pytest.raises(LayerError):
            layers.base_layers(name)


def _docs_gate() -> str:
    data = yaml.safe_load((layers.STARTERS / "docs-only" / "config.yaml").read_text(encoding="utf-8"))
    return data["gates"]["whitespace"]["run"]


def _gate(repo: Path):
    import subprocess

    return subprocess.run(_docs_gate(), shell=True, cwd=repo, capture_output=True, text=True)


def test_the_docs_only_gate_passes_on_an_unborn_repo_and_a_markdown_line_break(tmp_path):
    import subprocess

    def git(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=tmp_path,
                       check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    assert _gate(tmp_path).returncode == 0                                   # an unborn HEAD, nothing tracked
    (tmp_path / "a.md").write_text("line one  \nline two\n", encoding="utf-8")  # a Markdown line break
    git("add", "a.md")
    assert _gate(tmp_path).returncode == 0                                   # still unborn, one staged file
    git("commit", "-q", "-m", "docs")
    assert _gate(tmp_path).returncode == 0                                   # a committed tree
    (tmp_path / "b.md").write_text("x\n<<<<<<< HEAD\ny\n=======\nz\n>>>>>>> other\n", encoding="utf-8")
    git("add", "b.md")
    git("commit", "-q", "-m", "broken")
    proc = _gate(tmp_path)
    assert proc.returncode != 0 and "conflict marker" in proc.stdout + proc.stderr


def test_policy_obligations_merge_by_id():
    engine = L("engine", {"policy": {"enforce": "report", "obligations": {
        "a": {"lanes": ["feature"], "require": ["step:verify"]},
        "b": {"lanes": ["bug"], "when": "security_path", "require": ["step:verify"]}}}})
    pack = L("project", {"policy": {"obligations": {
        "b": {"require": ["step:x"]},
        "c": {"require": ["gates:verify"]}}}})
    merged, origins = merge([engine, pack])
    obligations = merged["policy"]["obligations"]
    assert obligations["a"] == {"lanes": ["feature"], "require": ["step:verify"]}
    assert obligations["b"] == {"require": ["step:x"]}  # whole: nothing left of the engine's `b`
    assert obligations["c"] == {"require": ["gates:verify"]}
    assert merged["policy"]["enforce"] == "report"
    assert origins[("policy", "obligations", "a")] == "engine"
    assert origins[("policy", "obligations", "b")] == "project"
    assert origins[("policy", "enforce")] == "engine"


def test_a_null_obligation_switches_a_default_off():
    engine = L("engine", {"policy": {"obligations": {"a": {"require": ["step:verify"]}}}})
    merged, _ = merge([engine, L("project", {"policy": {"obligations": {"a": None}}})])
    assert merged["policy"]["obligations"] == {}
