"""`run_env`: the environment a run's agents and gates inherit, without the variables an IDE or shell leaked in."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from agentflow import run_env
from agentflow.config import ConfigError, load_config


@pytest.fixture
def main(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


@pytest.fixture
def make(main, tmp_cfg):
    def build(inherit: dict | None = None, env: dict | None = None):
        patch: dict = {"defaults": {}}
        if inherit is not None:
            patch["defaults"]["env_inherit"] = inherit
        if env is not None:
            patch["defaults"]["env"] = env
        return load_config(tmp_cfg(patch))

    return build


def names(rows) -> set[str]:
    return {name for name, _ in rows}


def test_the_default_drops_the_variables_that_repeat_the_dotenv_file(main, make):
    cfg = make()
    assert cfg.defaults.env_inherit.drop == [] and cfg.defaults.env_inherit.drop_dotenv == [".env"]
    (main / ".env").write_text("X_URL=a\nOTHER=1\n", encoding="utf-8")
    environ = {"X_URL": "a", "OTHER": "2", "PATH": "/bin"}
    assert run_env.base(cfg, environ) == {"OTHER": "2", "PATH": "/bin"}  # OTHER differs: the user set it on purpose
    assert run_env.dropped(cfg, environ) == [("X_URL", "same value as .env")]


def test_a_variable_with_another_value_than_the_dotenv_file_is_kept(main, make):
    cfg = make()
    (main / ".env").write_text("X_URL=a\n", encoding="utf-8")
    assert run_env.base(cfg, {"X_URL": "b"}) == {"X_URL": "b"}
    assert run_env.dropped(cfg, {"X_URL": "b"}) == []


def test_a_drop_glob_removes_the_matching_names(main, make):
    cfg = make({"drop": ["*_DATABASE_URL", "EXACT"], "drop_dotenv": []})
    environ = {"APP_DATABASE_URL": "u", "DATABASE_URL": "v", "EXACT": "1", "KEEP": "2"}
    assert run_env.base(cfg, environ) == {"DATABASE_URL": "v", "KEEP": "2"}  # `*_X` needs a prefix and an underscore
    assert names(run_env.dropped(cfg, environ)) == {"APP_DATABASE_URL", "EXACT"}


def test_the_names_a_run_needs_survive_both_kinds_of_drop(main, make):
    cfg = make({"drop": ["*"], "drop_dotenv": [".env"]})
    keep = {"PATH": "/bin", "HOME": "/h", "USER": "u", "LOGNAME": "u", "SHELL": "/bin/sh", "TMPDIR": "/t",
            "LANG": "C", "LC_ALL": "C", "LC_CTYPE": "C", "TERM": "xterm"}
    (main / ".env").write_text("".join(f"{k}={v}\n" for k, v in keep.items()), encoding="utf-8")
    assert run_env.base(cfg, {**keep, "OTHER": "x"}) == keep
    assert names(run_env.dropped(cfg, {**keep, "OTHER": "x"})) == {"OTHER"}


def test_defaults_env_still_sets_a_dropped_name_to_the_packs_value(main, make):
    cfg = make({"drop": ["X_URL"], "drop_dotenv": []}, env={"X_URL": "pack"})
    base = run_env.base(cfg, {"X_URL": "leaked"})
    assert {**base, **cfg.defaults.env}["X_URL"] == "pack"
    assert "leaked" not in base.values()


def test_a_symlinked_dotenv_file_is_ignored(main, make, tmp_path):
    cfg = make()
    elsewhere = tmp_path.parent / f"{tmp_path.name}-real-env"
    elsewhere.write_text("X_URL=a\n", encoding="utf-8")
    (main / ".env").symlink_to(elsewhere)
    assert run_env.base(cfg, {"X_URL": "a"}) == {"X_URL": "a"}


def test_a_dotenv_file_under_a_symlinked_folder_is_ignored(main, make, tmp_path):
    cfg = make({"drop": [], "drop_dotenv": ["conf/.env"]})
    real = tmp_path.parent / f"{tmp_path.name}-real-conf"
    real.mkdir()
    (real / ".env").write_text("X_URL=a\n", encoding="utf-8")
    (main / "conf").symlink_to(real, target_is_directory=True)
    assert run_env.base(cfg, {"X_URL": "a"}) == {"X_URL": "a"}


def test_an_oversized_dotenv_file_is_ignored(main, make):
    cfg = make()
    (main / ".env").write_text("X_URL=a\n" + "#" * run_env.MAX_DOTENV_BYTES, encoding="utf-8")
    assert run_env.base(cfg, {"X_URL": "a"}) == {"X_URL": "a"}


def test_a_dotenv_that_is_a_directory_or_missing_is_ignored(main, make):
    cfg = make()
    assert run_env.base(cfg, {"X_URL": "a"}) == {"X_URL": "a"}
    (main / ".env").mkdir()
    assert run_env.base(cfg, {"X_URL": "a"}) == {"X_URL": "a"}


@pytest.mark.parametrize("line, key, value", [
    ("K=v", "K", "v"),
    ("export K=v", "K", "v"),
    ("export   K = v w ", "K", "v w"),
    ('K="a b # c"', "K", "a b # c"),
    ("K='a b # c' # note", "K", "a b # c"),
    ("K=v # note", "K", "v"),
    ("K=v#notacomment", "K", "v#notacomment"),
    ('K="a\\"b"', "K", 'a"b'),
    ("K=", "K", ""),
    ("_k9=1", "_k9", "1"),
])
def test_dotenv_forms_parse(tmp_path, line, key, value):
    path = tmp_path / ".env"
    path.write_text(f"# comment\n\n{line}\n", encoding="utf-8")
    assert run_env._dotenv(path) == {key: value}


@pytest.mark.parametrize("line", ["1K=v", "K-X=v", "no equals", "=v", "K='unclosed", 'K="unclosed', "A B=c", "$(x)=1",
                                  "K" * 129 + "=v"])
def test_other_lines_are_ignored(tmp_path, line):
    path = tmp_path / ".env"
    path.write_text(line + "\nOK=1\n", encoding="utf-8")
    assert run_env._dotenv(path) == {"OK": "1"}


def test_a_ten_thousand_character_line_returns_quickly(tmp_path):
    path = tmp_path / ".env"
    for line in ("A=" + '"' * 10000, "A=" + " #" * 5000, "export " * 1500 + "A=1", "A" * 10000, "A=" + "\\" * 10000,
                 'A="' + "\\\"" * 5000):
        path.write_text(line + "\nB=2\n", encoding="utf-8")
        started = time.monotonic()
        parsed = run_env._dotenv(path)
        assert time.monotonic() - started < 0.5
        assert parsed.get("B") == "2"


def test_the_parse_is_cached_until_the_file_changes(main, make):
    cfg = make()
    (main / ".env").write_text("X_URL=a\n", encoding="utf-8")
    assert run_env.dropped(cfg, {"X_URL": "a"})
    (main / ".env").write_text("X_URL=bb\n", encoding="utf-8")  # another size: the cache key changes
    assert run_env.dropped(cfg, {"X_URL": "a"}) == []


def test_base_defaults_to_this_process_environment(main, make, monkeypatch):
    cfg = make({"drop": ["LEAKED_*"], "drop_dotenv": []})
    monkeypatch.setenv("LEAKED_ONE", "x")
    monkeypatch.setenv("KEPT_ONE", "y")
    base = run_env.base(cfg)
    assert "LEAKED_ONE" not in base and base["KEPT_ONE"] == "y" and base["PATH"] == os.environ["PATH"]


def test_a_malformed_env_inherit_is_a_config_error(tmp_cfg):
    with pytest.raises(ConfigError):
        load_config(tmp_cfg({"defaults": {"env_inherit": {"drop": "A"}}}))
    with pytest.raises(ConfigError):
        load_config(tmp_cfg({"defaults": {"env_inherit": ["A"]}}))
