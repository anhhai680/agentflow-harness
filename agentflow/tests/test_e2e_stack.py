"""`devtools/e2e_stack.py` run for real in a throwaway checkout, with `docker` and `pnpm` faked on PATH."""

from __future__ import annotations

import fcntl
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "e2e_stack.py"
SECRET = "POSTGRES_PASSWORD=never-print-me"

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"tool": "docker", "args": args,
                          "project": os.environ.get("COMPOSE_PROJECT_NAME"),
                          "pg_port": os.environ.get("POSTGRES_HOST_PORT"),
                          "app_env": os.environ.get("APP_ENV")}) + "\n")
if args[:1] == ["info"]:
    sys.exit(int(os.environ.get("FAKE_DOCKER_INFO_EXIT", "0")))
if args[:1] == ["ps"]:
    print(os.environ.get("FAKE_DOCKER_PS", ""))
    sys.exit(0)
if "up" in args:
    sys.exit(int(os.environ.get("FAKE_DOCKER_UP_EXIT", "0")))
if "printenv" in args:
    sys.exit(1)  # the migration URL is absent, as SC 12 wants
sys.exit(0)
'''

FAKE_PNPM = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"tool": "pnpm", "args": args}) + "\n")
if args[-1:] != ["chromium"] and "test" in args:
    sys.exit(int(os.environ.get("FAKE_PLAYWRIGHT_EXIT", "0")))
sys.exit(0)
'''


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def health() -> Iterator[str]:
    class Ok(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')

        def log_message(self, *args: object) -> None:
            del args  # keep the test output quiet

    server = http.server.HTTPServer(("127.0.0.1", 0), Ok)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/api/health"
    server.shutdown()


@pytest.fixture
def checkout(tmp_path: Path, health: str) -> dict:
    root = tmp_path / "wt"
    (root / "devtools").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "devtools" / "e2e_stack.py")
    (root / ".env.example").write_text(f"APP_ENV=development\n{SECRET}\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("pnpm", FAKE_PNPM)):
        path = bin_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_LOG": str(log),
        "E2E_LOCK_FILE": str(tmp_path / "e2e.lock"),
        "E2E_LOCK_WAIT_S": "0.3",
        "E2E_PORTS": str(free_port()),
        "E2E_HEALTH_URL": health,
        "E2E_HEALTH_TRIES": "3",
        "E2E_HEALTH_INTERVAL_S": "0.05",
        "POSTGRES_HOST_PORT": "55432",  # what agentflow's defaults.env hands every gate
        "APP_ENV": "production",  # a developer's export, which .env.example's value must beat
    }
    env.pop("COMPOSE_PROJECT_NAME", None)
    return {"root": root, "env": env, "log": log, "lock": tmp_path / "e2e.lock"}


def run(checkout: dict, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(checkout["root"] / "devtools" / "e2e_stack.py"), *args],
                          cwd=checkout["root"], env={**checkout["env"], **env},
                          capture_output=True, text=True, timeout=60)


def calls(checkout: dict) -> list[dict]:
    if not checkout["log"].exists():
        return []
    return [json.loads(line) for line in checkout["log"].read_text(encoding="utf-8").splitlines()]


def docker_verbs(checkout: dict) -> list[str]:
    out = []
    for call in calls(checkout):
        if call["tool"] != "docker":
            continue
        args = call["args"]
        prefix = ["compose", "-p", "tivacare-e2e", "--env-file", ".env.example"]
        out.append(args[len(prefix)] if args[: len(prefix)] == prefix else args[0])
    return out


def test_a_clean_run_starts_seeds_tests_and_removes_the_stack(checkout):
    result = run(checkout, "e2e/settings")

    assert result.returncode == 0, result.stdout + result.stderr
    assert docker_verbs(checkout) == ["info", "down", "up", "exec", "exec", "exec", "exec", "down"]
    seed = [c["args"] for c in calls(checkout) if "seed" in " ".join(c["args"])]
    assert seed == [["compose", "-p", "tivacare-e2e", "--env-file", ".env.example", "exec", "-T", "api", "python",
                     "-m", "tivacare.entrypoints.seed"]]
    pnpm = [c["args"] for c in calls(checkout) if c["tool"] == "pnpm"]
    assert pnpm[-1] == ["--dir", "webadmin", "exec", "playwright", "test", "e2e/settings"]


def test_the_stack_runs_under_one_project_on_a_postgres_port_off_the_test_database(checkout):
    run(checkout)

    up = next(c for c in calls(checkout) if "up" in c["args"])
    assert up["project"] == "tivacare-e2e"
    assert up["pg_port"] == "55433"


def test_the_stack_is_configured_from_the_example_only(checkout):
    result = run(checkout)

    assert result.returncode == 0
    assert not (checkout["root"] / ".env").exists()  # nothing is created
    up = next(c for c in calls(checkout) if "up" in c["args"])
    assert up["app_env"] is None  # the exported APP_ENV is dropped: .env.example's value applies
    assert "never-print-me" not in result.stdout + result.stderr


def test_a_developers_own_env_file_is_left_alone(checkout):
    env_file = checkout["root"] / ".env"
    env_file.write_text("STAFF_TENANT_ID=\n", encoding="utf-8")  # a stale one, as in the run that found this

    assert run(checkout).returncode == 0
    assert env_file.read_text(encoding="utf-8") == "STAFF_TENANT_ID=\n"
    up = next(c for c in calls(checkout) if "up" in c["args"])
    assert up["args"][3:5] == ["--env-file", ".env.example"]


def test_a_missing_example_is_an_environment_problem(checkout):
    (checkout["root"] / ".env.example").unlink()
    result = run(checkout)

    assert result.returncode == 2
    assert ".env.example is missing" in result.stdout


def test_a_playwright_failure_keeps_its_exit_code_and_still_removes_the_stack(checkout):
    result = run(checkout, FAKE_PLAYWRIGHT_EXIT="1")

    assert result.returncode == 1
    verbs = docker_verbs(checkout)
    assert verbs[-3:] == ["ps", "logs", "down"]  # diagnostics first, then the stack comes down


def test_a_taken_port_is_an_environment_problem_and_nothing_starts(checkout):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        result = run(checkout, E2E_PORTS=str(port), FAKE_DOCKER_PS="someone-else-caddy-1")

    assert result.returncode == 2
    assert f"{port} by container someone-else-caddy-1" in result.stdout
    assert "up" not in docker_verbs(checkout)


def test_a_held_lock_waits_then_names_the_holder(checkout):
    with checkout["lock"].open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write("pid 4242 in /elsewhere\n")
        handle.flush()
        result = run(checkout)

    assert result.returncode == 2
    assert "pid 4242 in /elsewhere" in result.stdout
    assert calls(checkout) == []


def test_the_lock_is_free_again_after_a_run(checkout):
    assert run(checkout).returncode == 0
    assert run(checkout).returncode == 0


def test_docker_not_running_is_an_environment_problem(checkout):
    result = run(checkout, FAKE_DOCKER_INFO_EXIT="1")

    assert result.returncode == 2
    assert "Docker is not running" in result.stdout
    assert docker_verbs(checkout) == ["info"]


def test_a_stack_that_never_gets_healthy_is_removed(checkout):
    result = run(checkout, E2E_HEALTH_URL=f"http://127.0.0.1:{free_port()}/api/health")

    assert result.returncode == 2
    assert "never answered" in result.stdout
    assert docker_verbs(checkout)[-1] == "down"
    assert not any(c["tool"] == "pnpm" and "test" in c["args"] for c in calls(checkout))
