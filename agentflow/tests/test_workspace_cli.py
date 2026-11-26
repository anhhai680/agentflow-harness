"""`-C`, `--all` on the read-only views and `workspace list`: each runs the command's own function once per
repository of the user's workspace, so a single repository's output stays what it was."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
import yaml

from agentflow import flow, layers, repo_set, views
from agentflow.tests.conftest import SPARSE_PACK, run_git


def make_repo(parent: Path, name: str, with_pack: bool = True) -> Path:
    path = parent / name
    path.mkdir(parents=True)
    run_git(path, "init", "-q", "-b", "main")
    if with_pack:
        (path / ".agentflow").mkdir()
        (path / ".agentflow" / "config.yaml").write_text(yaml.safe_dump(SPARSE_PACK, sort_keys=False), encoding="utf-8")
    return path


def write_user_file(roots: list[Path]) -> None:
    path = layers.user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"workspace": {"roots": [str(r) for r in roots]}}), encoding="utf-8")


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """`ws/alpha` and `ws/beta` (with a pack), `ws/gamma` (none); a user file naming `ws`; cwd is not a repository."""
    root = tmp_path / "ws"
    root.mkdir()
    made = {name: make_repo(root, name, with_pack=name != "gamma") for name in ("alpha", "beta", "gamma")}
    write_user_file([root])
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    return root, made


@pytest.fixture
def cli(capsys):
    def run(*argv: str) -> tuple[int, str]:
        capsys.readouterr()
        code = flow.main(list(argv))
        return code, capsys.readouterr().out

    return run


def heading(repo: Path) -> str:
    return f"== {repo.name} (-C {shlex.quote(str(repo.resolve()))}) =="


def test_dash_c_runs_the_command_in_that_repository(ws, cli, monkeypatch):
    _, made = ws
    code, via_c = cli("-C", str(made["alpha"]), "status")
    assert code == 0
    monkeypatch.chdir(made["alpha"])
    assert cli("status") == (0, via_c)
    assert via_c == "No runs yet.\n"


def test_dash_c_with_a_bad_path_is_an_error(ws, cli, tmp_path):
    missing = tmp_path / "missing"
    code, out = cli("-C", str(missing), "status")
    assert code == 1
    assert out.startswith("error:") and str(missing) in out


def test_status_all_runs_once_per_repository_under_a_heading(ws, cli):
    _, made = ws
    code, out = cli("status", "--all")
    assert code == 0
    expected = [heading(made["alpha"]), "No runs yet.", heading(made["beta"]), "No runs yet.", heading(made["gamma"]),
                "not set up"]
    at = 0
    for needle in expected:
        at = out.index(needle, at) + len(needle)
    assert out.count("No runs yet.") == 2


def test_all_leaves_the_working_directory_alone(ws, cli):
    before = Path.cwd()
    cli("status", "--all")
    assert Path.cwd() == before


def test_a_broken_pack_does_not_stop_the_others(ws, cli):
    _, made = ws
    (made["beta"] / ".agentflow" / "config.yaml").write_text("gates: [unclosed\n", encoding="utf-8")
    before = Path.cwd()
    code, out = cli("agents", "--all")
    assert code == 1
    assert Path.cwd() == before
    alpha, beta, gamma = (out.index(heading(made[n])) for n in ("alpha", "beta", "gamma"))
    assert alpha < beta < gamma
    assert "error:" in out[beta:gamma]
    assert "error:" not in out[alpha:beta]
    assert "RUN" in out[alpha:beta] or "No agents" in out[alpha:beta] or out[alpha:beta].strip().count("\n") >= 1
    assert "not set up" in out[gamma:]


def test_an_unexpected_exception_in_one_repository_is_one_error_line(ws, cli, monkeypatch):
    _, made = ws
    real = views.agents_view

    def view(repo, cfg):
        if Path(repo).name == "alpha":
            raise RuntimeError("boom")
        return real(repo, cfg)

    monkeypatch.setattr(views, "agents_view", view)
    code, out = cli("agents", "--all")
    assert code == 1
    assert out.count("error: alpha: RuntimeError: boom") == 1
    assert out.index(heading(made["beta"])) > out.index("error: alpha: RuntimeError: boom")
    assert "Traceback" not in out


def test_all_with_no_workspace_says_where_to_set_it(tmp_path, monkeypatch, cli):
    monkeypatch.chdir(tmp_path)
    code, out = cli("status", "--all")
    assert code == 1
    assert "workspace.roots" in out


def test_all_refuses_c_config_watch_and_a_run_argument(ws, cli, tmp_path):
    _, made = ws
    for argv, why in (
        (("-C", str(made["alpha"]), "status", "--all"), "-C"),
        (("status", "--all", "--config", str(tmp_path / "f")), "--config"),
        (("status", "--all", "--watch"), "--watch"),
        (("status", "some-run", "--all"), "run"),
    ):
        code, out = cli(*argv)
        assert code == 1, argv
        assert out.startswith("error:") and why in out, argv
        assert "==" not in out, argv


def test_inbox_all_and_next_all_and_doctor_all_run_per_repository(ws, cli):
    _, made = ws
    code, out = cli("inbox", "--all")
    assert code == 0
    assert out.count("Nothing is waiting on you.") == 2
    for command in ("inbox", "next", "doctor"):
        _, out = cli(command, "--all")
        assert heading(made["alpha"]) in out and heading(made["beta"]) in out, command
        assert heading(made["gamma"]) in out and "not set up" in out, command


def test_a_folder_name_with_a_newline_is_printed_escaped(tmp_path, monkeypatch, cli):
    root = tmp_path / "ws"
    root.mkdir()
    make_repo(root, "evil\nname")
    write_user_file([root])
    monkeypatch.chdir(tmp_path)
    code, out = cli("status", "--all")
    assert code == 0
    line = next(ln for ln in out.splitlines() if ln.startswith("== evil"))
    assert "evil\\nname" in line and "evil\\nname" in line.split("(-C ", 1)[1]
    assert "name (-C" not in out.replace(line, "")  # the heading is one line


def test_workspace_list_text_and_json(ws, cli, tmp_path):
    root, made = ws
    write_user_file([root, tmp_path / "nowhere"])
    code, out = cli("workspace", "list")
    assert code == 0
    assert str(root) in out
    for name, state in (("alpha", "live"), ("beta", "live"), ("gamma", "not set up")):
        line = next(ln for ln in out.splitlines() if ln.lstrip().startswith(name))
        assert state in line and str(made[name].resolve()) in line
    assert any(ln.startswith("skipped ") and "nowhere" in ln for ln in out.splitlines())
    code, raw = cli("workspace", "list", "--json")
    assert code == 0
    data = json.loads(raw)
    assert set(data) == {"repos", "skipped"}
    assert [(r["name"], r["state"]) for r in data["repos"]] == [("alpha", "live"), ("beta", "live"),
                                                                ("gamma", "not_set_up")]
    assert set(data["repos"][0]) == {"id", "name", "path", "state"}
    assert data["repos"][0]["path"] == str(made["alpha"].resolve())
    assert data["skipped"] == [{"entry": str(tmp_path / "nowhere"), "reason": "is not a folder"}]


def test_workspace_list_with_no_workspace_returns_the_all_message(tmp_path, monkeypatch, cli):
    monkeypatch.chdir(tmp_path)
    code, out = cli("workspace", "list")
    code_all, out_all = cli("status", "--all")
    assert (code, code_all) == (1, 1)
    assert out == out_all and "workspace.roots" in out


def test_config_show_effective_reports_the_workspace_layer(ws, cli):
    root, made = ws
    code, out = cli("-C", str(made["alpha"]), "config", "show", "--effective", "workspace")
    assert code == 0
    assert f'workspace.roots: ["{root}"]  [user]' in out


def test_a_repository_path_with_a_space_is_quoted_in_the_heading(tmp_path, monkeypatch, cli):
    root = tmp_path / "my ws"
    root.mkdir()
    spaced = make_repo(root, "my repo")
    write_user_file([root])
    monkeypatch.chdir(tmp_path)
    code, out = cli("status", "--all")
    assert code == 0
    line = next(ln for ln in out.splitlines() if ln.startswith("== my repo"))
    assert f"(-C {shlex.quote(str(spaced.resolve()))})" in line
    assert shlex.split(line.split("(-C ", 1)[1].removesuffix(") =="))[0] == str(spaced.resolve())


@pytest.mark.parametrize("argv", [("metrics", "--all"), ("backlog", "list", "--all")])
@pytest.mark.parametrize("configured", [False, True])
def test_metrics_and_backlog_all_flags_are_not_the_workspace_flag(ws, cli, monkeypatch, argv, configured):
    """`metrics --all` (every run) and `backlog list --all` (dropped items) predate the workspace and keep meaning
    what they did: no heading, no fan-out, and the same output with a workspace configured or not."""
    _, made = ws
    monkeypatch.chdir(made["alpha"])
    layers.user_config_path().unlink()
    bare = cli(*argv)
    if configured:
        write_user_file([made["alpha"].parent])
        assert cli(*argv) == bare
    code, out = bare
    assert "no workspace" not in out and "==" not in out and "not set up" not in out


def test_a_setup_failure_in_all_is_the_standard_error_not_a_traceback(ws, cli, monkeypatch):
    def boom():
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(repo_set, "from_user_config", boom)
    code, out = cli("status", "--all")
    assert code == 1
    assert out.startswith("error: the runner stopped on an unexpected RuntimeError: Could not determine home")


def test_the_no_workspace_message_survives_a_missing_home_directory(tmp_path, monkeypatch, cli):
    def no_home():
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(layers, "user_config_path", no_home)
    monkeypatch.setattr(layers, "load_user_settings",
                        lambda: layers.UserSettings(layers.WorkspaceSettings((), (), ()), {}, None))
    code, out = cli("status", "--all")
    assert code == 1
    assert out == "error: no workspace: set workspace.roots in your user config file\n"
