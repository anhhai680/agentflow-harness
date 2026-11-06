"""`layers`: the merge of the config layers, `null` deletion, phase knobs, and the user file's personal-only rule."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

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
                                 "phases.build.needs_approval", "dashboard"])
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
