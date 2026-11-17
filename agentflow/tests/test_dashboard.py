"""The local dashboard (Task 13): token and origin checks, the JSON API, actions, SSE and the page."""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest

from agentflow import hitl, pack
from agentflow.config import RETRO_PIPELINE
from agentflow.dashboard import PAGE_CSP, UI_DIR, UI_SCRIPTS, make_server
from agentflow.run_control import RUN_ACTIONS, available_actions, cancel, pause
from agentflow.state import Run, runs_root
from agentflow.views import _badge, step_title
from conftest import PYTHON_CONFIG, run_git

TOKEN = "k3y-" + "x" * 40
AGENTFLOW = Path(__file__).resolve().parents[1]


def page_source() -> str:
    """The page shell followed by each of its scripts, in load order: what the source-pinning tests read."""
    return "\n".join([(AGENTFLOW / "dashboard.html").read_text(encoding="utf-8"),
                      *((UI_DIR / name).read_text(encoding="utf-8") for name in UI_SCRIPTS)])


@dataclass
class Dash:
    server: object
    port: int
    flow: object
    spawned: list = field(default_factory=list)

    def request(self, method: str, path: str, body: bytes | dict | None = None, headers: dict | None = None,
                token: str | None = TOKEN) -> tuple[int, dict, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        sent = dict(headers or {})
        if token is not None and "X-Agentflow-Token" not in sent:
            sent["X-Agentflow-Token"] = token
        payload = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
        if payload is not None:
            sent.setdefault("Content-Type", "application/json")
        try:
            conn.request(method, path, body=payload, headers=sent)
            response = conn.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            conn.close()

    def get(self, path: str, **kw) -> tuple[int, dict, bytes]:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: dict | bytes | None = None, **kw) -> tuple[int, dict, bytes]:
        return self.request("POST", path, body=body if body is not None else {}, **kw)


@pytest.fixture
def dash(flow_repo, monkeypatch):
    spawned: list[dict] = []
    real_popen = subprocess.Popen

    def popen(command, *args, **kwargs):
        if isinstance(command, list) and "resume" in command:
            spawned.append({"command": command, **kwargs})
            return None
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    server = make_server(flow_repo.root, flow_repo.cfg, "127.0.0.1", 0, TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield Dash(server, server.server_address[1], flow_repo, spawned)
    server.close()
    thread.join(timeout=5)


def awaiting(flow_repo, module: str = "crm", phase: str = "plan") -> Run:
    run = flow_repo.new_run({"module": module})
    run.emit("phase_started", phase=phase)
    run.emit("phase_approval_raised", phase=phase, round=0, summary=f"Phase {phase} is done")
    return run


def ready(flow_repo, module: str = "crm", phase: str = "plan", lane: str = "feature") -> Run:
    params = {"topic": module} if lane == "design" else {"module": module}
    run = Run.create(flow_repo.cfg, flow_repo.root, lane, params)
    run.emit("phase_started", phase=phase)
    run.emit("phase_approval_raised", phase=phase, round=0, summary=f"Phase {phase} is done")
    hitl.approve(run, phase, by="me", hold=True)
    return run


def with_work(flow_repo, run: Run) -> Run:
    """Give `run` a checkout with one commit since its base, as `mark_done` needs."""
    base = flow_repo.head()
    (flow_repo.root / "work.txt").write_text("work\n", encoding="utf-8")
    run_git(flow_repo.root, "add", "-A")
    run_git(flow_repo.root, "commit", "-q", "-m", "work")
    run.emit("run_updated", worktree=str(flow_repo.root), base_sha=base, branch="flow/feature/crm")
    return run


def card_of(dash) -> dict:
    (card,) = payload(dash.get("/api/board")[2])["cards"]
    return card


def payload(body: bytes) -> dict:
    return json.loads(body.decode("utf-8"))


def new_events(run: Run, after: int) -> list[dict]:
    return [{k: v for k, v in e.items() if k not in ("seq", "ts", "by")} for e in run.events.read(after_seq=after)]


# --------------------------------------------------------------------------- the token and the request checks


def test_info_names_the_engine_version(dash):
    status, headers, body = dash.get("/api/info")
    assert status == 200 and headers["content-type"].startswith("application/json")
    assert json.loads(body) == {"name": "agentflow-harness", "version": pack.ENGINE_VERSION, "board_refresh_s": 2.0}
    assert dash.get("/api/info", token=None)[0] == 403
    page = page_source()
    assert 'id="version"' in page and "/api/info" in page


def test_info_carries_the_board_refresh(dash):
    assert payload(dash.get("/api/info")[2])["board_refresh_s"] == 2.0
    dash.server.cfg.dashboard["board_refresh_s"] = 7
    assert payload(dash.get("/api/info")[2])["board_refresh_s"] == 7


def test_info_ignores_an_unusable_board_refresh(dash):
    for bad in ("x", True, 0.01, 1000, float("nan")):
        dash.server.cfg.dashboard["board_refresh_s"] = bad
        assert payload(dash.get("/api/info")[2])["board_refresh_s"] == 2.0, bad
    dash.server.cfg.dashboard["board_refresh_s"] = 5
    assert payload(dash.get("/api/info")[2])["board_refresh_s"] == 5.0


def test_the_open_tab_reloads_on_a_second_click():
    main = (UI_DIR / "main.js").read_text(encoding="utf-8")
    assert 'if (location.hash === "#/" + name) route();' in main


def test_api_rejects_without_token(dash):
    status, headers, body = dash.get("/api/board", token=None)
    assert status == 403 and body == b""
    assert dash.get("/api/board", token="wrong")[0] == 403
    assert dash.get("/api/board", token="")[0] == 403
    assert dash.get("/api/events", token=None)[0] == 403
    assert dash.post("/api/runs/x/approve", token=None)[0] == 403
    assert dash.get("/no-such-page", token=None)[0] == 403  # nothing, not even a 404, without the token
    status, headers, body = dash.get("/api/board")
    assert status == 200 and headers["content-type"].startswith("application/json")
    assert payload(body)["columns"][0] == "Backlog"
    assert dash.get(f"/api/board?token={TOKEN}", token=None)[0] == 200
    assert dash.get("/api/agents")[0] == 200


def test_the_board_names_the_work_source_kind_and_the_skipped_files(dash):
    data = payload(dash.get("/api/board")[2])
    assert data["backlog_skipped"] == [] and data["work_source_kind"] == "capability_map"


def test_security_headers_on_every_response(dash):
    for status, headers, _ in (dash.get("/api/board"), dash.get("/api/board", token=None), dash.get("/", token=None)):
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["cache-control"] == "no-store"
        assert headers["referrer-policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert "default-src 'none'" in headers["content-security-policy"]


def test_bad_host_is_refused(dash):
    assert dash.get("/api/board", headers={"Host": "evil.example"})[0] == 403
    assert dash.get("/api/board", headers={"Host": f"evil.example:{dash.port}"})[0] == 403
    assert dash.get("/api/board", headers={"Host": "127.0.0.1:1"})[0] == 403
    assert dash.get("/api/board", headers={"Host": f"localhost:{dash.port}"})[0] == 200


def test_bad_origin_is_refused(dash):
    run = awaiting(dash.flow)
    status, _, body = dash.post(f"/api/runs/{run.id}/pause", headers={"Origin": "https://evil.example"})
    assert status == 403 and body == b""
    assert dash.post(f"/api/runs/{run.id}/pause", headers={"Origin": "null"})[0] == 403
    assert dash.post(f"/api/runs/{run.id}/pause", headers={"Origin": f"http://127.0.0.1:{dash.port}"})[0] == 200
    run.refresh()
    assert [e["by"] for e in dash.flow.events(run, "run_paused")] == ["dashboard"]


def test_post_bodies_are_json_and_capped(dash):
    run = awaiting(dash.flow)
    url = f"/api/runs/{run.id}/reject"
    assert dash.post(url, b"note=x", headers={"Content-Type": "application/x-www-form-urlencoded"})[0] == 415
    assert dash.post(url, b"{not json")[0] == 400
    assert dash.post(url, b"[1, 2]")[0] == 400
    big = json.dumps({"note": "x" * (65 * 1024)}).encode("utf-8")
    assert dash.post(url, big)[0] == 413
    status, _, body = dash.post(url, {"note": 42})
    assert status == 400 and "note" in payload(body)["error"]
    run.refresh()
    assert run.state["refine"] is None and dash.spawned == []


def test_run_ids_cannot_escape_the_runs_root(dash):
    awaiting(dash.flow)
    for path in ("/api/runs/..%2F..%2Fetc%2Fpasswd", "/api/runs/../../etc/passwd", "/api/runs/%2e%2e",
                 "/api/runs/20261030-0000-feature-nope", "/api/runs/20261030-0000-feature-crm%00",
                 "/api/runs/crm"):  # a part of an id is not an id here
        assert dash.get(path)[0] == 404, path
    assert dash.post("/api/runs/..%2F..%2Fx/approve")[0] == 404
    assert dash.get("/api/runs")[0] == 404


def test_unknown_routes_and_actions(dash):
    run = awaiting(dash.flow)
    assert dash.get("/api/nothing")[0] == 404
    assert dash.post(f"/api/runs/{run.id}/explode")[0] == 404
    assert dash.request("DELETE", f"/api/runs/{run.id}")[0] in (403, 404, 405, 501)
    assert dash.get("/api/board?lane=nope")[0] == 404


# --------------------------------------------------------------------------- data


@pytest.mark.parametrize("to_phase, column, badge", [
    ("define", "define", "stopped after define"),
    ("ship", "Done", "done"),
    (None, "Done", "done"),
])
def test_a_finished_run_shows_where_it_stopped(dash, to_phase, column, badge):
    run = dash.flow.new_run()
    run.emit("run_options", to_phase=to_phase)
    run.emit("phase_started", phase=to_phase or "ship")
    run.emit("run_finished")
    card = payload(dash.get("/api/board")[2])["cards"][0]
    assert (card["column"], card["badge"]) == (column, badge)


@pytest.mark.parametrize("ending", ["stopped_after_define", "cancelled"])
def test_an_archived_run_moves_to_done_and_says_it_was_archived(dash, ending):
    run = dash.flow.new_run()
    if ending == "cancelled":
        run.emit("phase_started", phase="define")
        run.emit("run_cancelled", by="me")
    else:
        run.emit("run_options", to_phase="define")
        run.emit("phase_started", phase="define")
        run.emit("run_finished")
    assert payload(dash.get("/api/board")[2])["cards"][0]["column"] == "define"
    run.emit("run_archived", by="me")
    card = payload(dash.get("/api/board")[2])["cards"][0]
    assert (card["column"], card["badge"]) == ("Done", "archived")


def test_a_held_run_says_why_and_until_when(dash, monkeypatch):
    # `time.tzset` is missing on some Python builds (uv's macOS 3.12.11): the time then shows in the host's zone
    can_set_zone = hasattr(time, "tzset")
    if can_set_zone:
        monkeypatch.setenv("TZ", "Asia/Ho_Chi_Minh")
        time.tzset()
    until = "2026-10-31T09:30:00+00:00"
    run = dash.flow.new_run()
    run.emit("run_held", key="build/impl", reason="usage limit", until=until)
    lock = run.lock()
    assert lock.acquire()
    try:
        card = payload(dash.get("/api/board")[2])["cards"][0]
        local = datetime.fromisoformat(until).astimezone().strftime("%H:%M")
    finally:
        lock.release()
        if can_set_zone:
            monkeypatch.undo()
            time.tzset()
    if can_set_zone:
        assert local == "16:30"  # the badge shows local time, not UTC
    assert (card["status"], card["badge"]) == ("held", f"held until {local} (usage limit)")


def test_spend_today_counts_a_resumed_session_once(dash):
    run = dash.flow.new_run()
    run.emit("agent_exited", session_id="s-1", exit_code=0, cost=2.2046758)
    run.emit("agent_exited", session_id="s-1", exit_code=0, cost=2.3711376)
    assert payload(dash.get("/api/board")[2])["spend_today"] == 2.37


def test_a_card_shows_the_soft_and_the_hard_budget(dash):
    from agentflow.views import board_table, metrics_view

    defaults = dash.flow.cfg.defaults
    run = dash.flow.new_run()
    data = payload(dash.get("/api/board")[2])
    (card,) = data["cards"]
    assert (card["soft_budget"], card["budget"]) == (defaults.run_budget_usd, defaults.run_budget_hard_usd)
    assert (data["run_budget_usd"], data["run_budget_hard_usd"]) == (defaults.run_budget_usd, defaults.run_budget_hard_usd)
    assert f"$0.00/${defaults.run_budget_usd:.0f}·${defaults.run_budget_hard_usd:.0f}" in board_table(data)
    detail = payload(dash.get(f"/api/runs/{run.id}")[2])
    assert detail["facts"]["soft_budget_usd"] == defaults.run_budget_usd
    (row,) = metrics_view(dash.flow.root, dash.flow.cfg)["runs"]
    assert (row["soft_budget"], row["budget"]) == (defaults.run_budget_usd, defaults.run_budget_hard_usd)
    source = page_source()
    for needle in ("card.soft_budget", "facts.soft_budget_usd", "r.soft_budget", "data.run_budget_hard_usd"):
        assert needle in source


def test_board_and_run_detail(dash):
    planning = awaiting(dash.flow, "staff-auth")
    run_dir = planning.dir
    planning.progress_path().write_text("# Tiến độ\n\n- bước 1 xong\n", encoding="utf-8")
    (run_dir / "transcripts").mkdir(exist_ok=True)
    (run_dir / "transcripts" / "plan__plan.1.jsonl").write_text('{"type": "result"}\n' * 5000, encoding="utf-8")
    status, _, body = dash.get("/api/board")
    data = payload(body)
    assert status == 200 and [c["column"] for c in data["cards"]] == ["plan"]
    assert data["needs_human"][0]["key"] == "plan_approved"

    status, _, body = dash.get(f"/api/runs/{planning.id}")
    detail = payload(body)
    assert status == 200 and detail["run_id"] == planning.id
    assert "Tiến độ" in detail["progress"]
    assert len(detail["transcript"]) <= 16 * 1024 and detail["transcript"].endswith('{"type": "result"}\n')
    assert [e["type"] for e in detail["timeline"]][-1] == "phase_approval_raised"
    assert detail["attach"] == f"agentflow attach {planning.id}"


@pytest.mark.parametrize("key, title", [
    ("build/build/task-9/implement/resolve-2", "Task 9 · implement · resolver 2"),
    ("build/build/task-9/implement", "Task 9 · implement"),
    ("plan/plan", "plan"),
    ("define/draft", "define · draft"),
    ("verify/loop/r2/fix", "verify · loop · round 2 · fix"),
])
def test_a_step_key_reads_as_words(key, title):
    assert step_title(key) == title


def test_run_detail_is_slim_unless_raw_is_asked(dash):
    run = awaiting(dash.flow)
    detail = payload(dash.get(f"/api/runs/{run.id}")[2])
    assert "state" not in detail and detail["actions"] == ["approve", "reject", "pause", "cancel"]
    assert detail["label"] == "crm" and detail["waiting"]["kind"] == "phase_approval"
    rail = {p["phase"]: p for p in detail["stage_rail"]}
    assert rail["plan"]["note"] == "waiting on you" and rail["build"]["status"] == "not reached"
    assert "state" in payload(dash.get(f"/api/runs/{run.id}?raw=1")[2])


def test_the_rail_names_a_refine_round(dash):
    run = awaiting(dash.flow)
    hitl.reject(run, "plan", "me", "redo")
    rail = {p["phase"]: p for p in payload(dash.get(f"/api/runs/{run.id}")[2])["stage_rail"]}
    assert "refine 1" in rail["plan"]["note"]


def test_a_card_shows_a_review_a_refine_round_skipped(dash):
    run = dash.flow.new_run({"module": "crm"})
    run.emit("phase_started", phase="build")
    run.emit("step_started", key="build/build/task-2/implement", phase="build", task={"n": 2, "of": 3})
    run.emit("review_skipped", key="build/build/task-2/review", reason="unchanged", round=1)
    run.emit("review_skipped", key="plan/plan/task-1/review", reason="unchanged", round=1)  # not this phase's
    assert card_of(dash)["review_skipped"] == [{"key": "build/build/task-2/review", "task": 2, "reason": "unchanged"}]
    run.emit("review_result", key="build/build/task-2/review", round=1, findings=[], blocking=0, blocking_findings=[])
    assert card_of(dash)["review_skipped"] == []  # a review that ran replaces the skip
    source = page_source()
    assert re.search(r"review_skipped[^\n]*gate-skip", source)  # drawn in the existing skipped style


def test_tails_never_follow_symlinks_out_of_the_run(dash, tmp_path):
    run = awaiting(dash.flow)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET\n", encoding="utf-8")
    run.progress_path().symlink_to(secret)
    status, _, body = dash.get(f"/api/runs/{run.id}")
    assert status == 200 and b"TOP SECRET" not in body


def test_untrusted_text_is_escaped_in_json(dash):
    run = awaiting(dash.flow)
    note = "Sửa lại <script>alert(1)</script> & <img src=x onerror=alert(2)>"
    hitl.reject(run, "plan", "me", note)
    status, _, body = dash.get(f"/api/runs/{run.id}")
    assert status == 200 and b"<script>" not in body and b"<img" not in body
    assert payload(body)["facts"]["refine"]["note"] == note


def test_page_is_served_with_a_nonce_and_no_unsafe_dom_sinks(dash):
    status, headers, body = dash.get("/", token=None)
    assert status == 200 and headers["content-type"].startswith("text/html")
    csp = headers["content-security-policy"]
    nonce = re.search(r"'nonce-([A-Za-z0-9_\-]+)'", csp).group(1)
    page = body.decode("utf-8")
    assert f'<script nonce="{nonce}" src="/ui/core.js"></script>' in page and "{{" not in page
    assert PAGE_CSP == ("default-src 'none'; script-src 'nonce-{n}'; style-src 'nonce-{n}'; connect-src 'self'; "
                        "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    assert "connect-src 'self'" in csp and "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp
    assert dash.get("/", token=None)[1]["content-security-policy"] != csp  # a fresh nonce each time

    source = page_source()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                 "setTimeout(\"", "setTimeout('", " style=\"", "javascript:"):
        assert sink not in source, sink
    assert not re.search(r"https?://", source)  # no external scripts, fonts or requests
    assert "textContent" in source and "EventSource" in source and "prefers-color-scheme" in source
    assert "X-Agentflow-Token" in source


def test_the_page_loads_each_ui_script_once_in_order(dash):
    status, headers, body = dash.get("/", token=None)
    nonce = re.search(r"'nonce-([A-Za-z0-9_\-]+)'", headers["content-security-policy"]).group(1)
    tags = re.findall(r'<script nonce="([^"]+)" src="/ui/([a-z_]+\.js)"></script>', body.decode("utf-8"))
    assert tags == [(nonce, name) for name in UI_SCRIPTS]
    assert "<script nonce" not in body.decode("utf-8").replace('<script nonce="%s" src=' % nonce, "")  # no inline script


def test_every_ui_file_is_listed():
    assert sorted(p.name for p in UI_DIR.glob("*.js")) == sorted(UI_SCRIPTS)
    assert UI_SCRIPTS[0] == "core.js" and UI_SCRIPTS[-1] == "main.js"


def test_markdown_loads_before_the_run_page():
    assert UI_SCRIPTS.index("markdown.js") < UI_SCRIPTS.index("run.js")
    assert UI_SCRIPTS.index("markdown.js") == UI_SCRIPTS.index("dialogs.js") + 1
    assert "innerHTML" not in (UI_DIR / "markdown.js").read_text(encoding="utf-8")


def test_ui_scripts_need_no_token_but_keep_the_host_check(dash):
    status, headers, body = dash.get("/ui/core.js", token=None)
    assert status == 200 and headers["content-type"] == "text/javascript; charset=utf-8"
    assert headers["cache-control"] == "no-store" and headers["x-content-type-options"] == "nosniff"
    assert b"const TOKEN" in body
    assert dash.get("/ui/core.js", token=None, headers={"Host": "evil.example"})[0] == 403


@pytest.mark.parametrize("path", ["/ui/evil.js", "/ui/../dashboard.py", "/ui/%2e%2e/dashboard.py",
                                  "/ui/core.js/", "/ui/CORE.js", "/ui/dashboard.html"])
def test_only_listed_ui_scripts_are_served(dash, path):
    assert dash.get(path)[0] == 404


# --------------------------------------------------------------------------- actions


def test_dashboard_approve_equals_cli(dash):
    cli_run = awaiting(dash.flow, "cli")
    web_run = awaiting(dash.flow, "web")
    cli_after, web_after = cli_run.events.last_seq(), web_run.events.last_seq()
    hitl.approve(cli_run, "plan", by="someone", hold=True)
    status, _, body = dash.post(f"/api/runs/{web_run.id}/approve", {"target": "plan"})
    assert status == 200 and payload(body)["resumed"] is False
    assert new_events(web_run, web_after) == new_events(cli_run, cli_after)
    assert [e["by"] for e in web_run.events.read(after_seq=web_after)] == ["dashboard", "dashboard"]
    web_run.refresh()
    assert web_run.state["phases"]["plan"]["status"] == "approved" and web_run.state["waiting"] is None
    assert dash.spawned == []

    status, _, body = dash.post(f"/api/runs/{web_run.id}/approve", {"target": "plan"})  # a second actor
    assert status == 409 and dash.spawned == []


def test_approve_records_what_the_page_showed(dash):
    run = awaiting(dash.flow, phase="define")
    viewed = [{"path": "docs/specs/x.md", "sha": "a" * 40}]
    assert dash.post(f"/api/runs/{run.id}/approve", {"viewed": viewed})[0] == 200
    (event,) = [e for e in run.events.read() if e["type"] == "phase_approved"]
    assert event["viewed"] == viewed and event["by"] == "dashboard"


@pytest.mark.parametrize("viewed", ["x", [{"path": "a"}], [{"path": "a", "sha": "zz"}],
                                    [{"path": "", "sha": "a" * 40}], [{"path": "a", "sha": "a" * 40, "x": 1}],
                                    [{"path": "a", "sha": "a" * 40}] * 101])
def test_a_malformed_viewed_is_a_400_and_approves_nothing(dash, viewed):
    run = awaiting(dash.flow, phase="define")
    assert dash.post(f"/api/runs/{run.id}/approve", {"viewed": viewed})[0] == 400
    assert not [e for e in run.events.read() if e["type"] == "phase_approved"]


def test_approving_a_phase_rests_the_card_where_it_is(dash):
    run = awaiting(dash.flow, phase="define")
    assert dash.post(f"/api/runs/{run.id}/approve")[0] == 200
    card = card_of(dash)
    assert (card["status"], card["column"], card["badge"]) == ("ready", "define", "ready")


def test_approving_a_stop_inside_a_phase_still_resumes_at_once(dash):
    run = dash.flow.new_run()
    run.emit("hitl_raised", kind="blocked", key="build/impl", summary="stuck")
    status, _, body = dash.post(f"/api/runs/{run.id}/approve")
    assert status == 200 and payload(body)["resumed"] is True
    assert len(dash.spawned) == 1


def test_continuing_a_ready_card_starts_one_detached_runner(dash):
    run = ready(dash.flow)
    status, _, body = dash.post(f"/api/runs/{run.id}/resume")
    assert status == 200 and payload(body)["resumed"] is True
    run.refresh()
    assert run.state["status"] == "running"
    (spawn,) = dash.spawned
    assert spawn["command"][-2:] == ["resume", run.id]
    assert spawn["command"][1].endswith("flow.py")
    assert spawn["start_new_session"] is True and spawn["stdin"] == subprocess.DEVNULL
    assert spawn["stdout"].name == str(run.dir / "runner.log") and spawn["cwd"] == run.repo


def test_marking_a_ready_card_done_records_it_and_starts_the_runner(dash):
    run = with_work(dash.flow, ready(dash.flow, phase="define"))
    status, _, body = dash.post(f"/api/runs/{run.id}/done")
    assert status == 200 and payload(body)["resumed"] is True
    run.refresh()
    assert run.state["marked_done"]["phase"] == "define" and run.state["marked_done"]["by"] == "dashboard"
    assert [s["command"][-2:] for s in dash.spawned] == [["resume", run.id]]


def test_marking_a_card_done_is_refused_unless_it_is_ready(dash):
    run = awaiting(dash.flow)
    status, _, body = dash.post(f"/api/runs/{run.id}/done")
    assert status == 409 and "not ready" in payload(body)["error"]
    assert dash.spawned == []


def test_marking_a_card_done_needs_the_token_and_our_origin(dash):
    run = with_work(dash.flow, ready(dash.flow))
    assert dash.post(f"/api/runs/{run.id}/done", token=None)[0] == 403
    assert dash.post(f"/api/runs/{run.id}/done", headers={"Origin": "https://evil.example"})[0] == 403
    run.refresh()
    assert run.state["status"] == "ready" and dash.spawned == []


@pytest.mark.parametrize("lane, phase, following", [
    ("feature", "define", ["plan", "build", "verify", "review"]),
    ("bug", "define", ["build", "verify", "review"]),
    ("feature", "review", []),
    ("design", "define", []),
])
def test_a_ready_card_names_the_phases_still_ahead(dash, lane, phase, following):
    ready(dash.flow, "x", phase, lane)
    assert card_of(dash)["ready"] == {"phase": phase, "next": following[0] if following else None, "skips": following}


def test_only_a_ready_card_offers_a_move(dash):
    awaiting(dash.flow)
    assert card_of(dash)["ready"] is None


def test_a_run_marked_done_lands_in_done_even_when_started_with_to(dash):
    run = dash.flow.new_run()
    run.emit("run_options", to_phase="define")
    run.emit("phase_started", phase="define")
    run.emit("phase_ready", phase="define")
    run.emit("run_marked_done", phase="define", by="me")
    run.emit("run_finished")
    card = card_of(dash)
    assert (card["column"], card["badge"]) == ("Done", "done")


@pytest.mark.parametrize("url, link", [
    ("https://github.com/acme/repo/pull/42", "https://github.com/acme/repo/pull/42"),
    ("javascript:alert(1)", None),
    ("https://github.com.evil.example/acme/repo/pull/42", None),
    ("http://github.com/acme/repo/pull/42", None),
])
def test_a_card_links_only_to_a_github_pull_request(dash, url, link):
    run = dash.flow.new_run()
    run.emit("pr_opened", url=url, key="ship/finish")
    assert card_of(dash)["pr_link"] == link


def test_dashboard_approve_defaults_to_what_the_run_waits_on(dash):
    run = awaiting(dash.flow)
    assert dash.post(f"/api/runs/{run.id}/approve")[0] == 200
    run.refresh()
    assert run.state["phases"]["plan"]["status"] == "approved"


def test_dashboard_reject_starts_refine(dash):
    run = awaiting(dash.flow)
    note = "Thêm bước kiểm tra mã khách hàng"
    status, _, body = dash.post(f"/api/runs/{run.id}/reject", {"note": note})
    assert status == 200
    run.refresh()
    assert run.state["refine"] == {"phase": "plan", "round": 1, "note": note}
    assert run.refine_note_path("plan", 1).read_text(encoding="utf-8").count(note) == 1
    assert [e["by"] for e in dash.flow.events(run, "phase_rejected")] == ["dashboard"]
    (spawn,) = dash.spawned
    assert spawn["command"][-2:] == ["resume", run.id]
    status, _, body = dash.post(f"/api/runs/{run.id}/reject", {"note": "   "})
    assert status in (400, 409)


def test_dashboard_answer(dash):
    run = dash.flow.new_run()
    questions = [hitl.Question("q1", "Bộ nhớ đệm bao lâu?", [hitl.Option("A", "1 giờ", True)], [], "spec_gap"),
                 hitl.Question("q2", "Ai duyệt?", [], [], "other")]
    hitl.raise_hitl(dash.flow.cfg, run, "needs_human", "define/draft", "Cần quyết định", questions)
    status, _, body = dash.post(f"/api/runs/{run.id}/answer", {"answers": {"q1": "A"}})
    assert status == 200 and payload(body) == {"ok": True, "complete": False, "pending": ["q2"], "resumed": False}
    assert dash.spawned == []
    status, _, body = dash.post(f"/api/runs/{run.id}/answer", {"key": "define/draft", "answers": {"q2": "Chị Hà"}})
    assert status == 200 and payload(body)["complete"] is True
    run.refresh()
    (answered,) = dash.flow.events(run, "hitl_answered")
    assert answered["answers"] == {"q1": "A", "q2": "Chị Hà"} and answered["by"] == "dashboard"
    assert run.state["waiting"] is None and len(dash.spawned) == 1
    assert dash.post(f"/api/runs/{run.id}/answer", {"answers": {"q1": 3}})[0] == 400


def test_dashboard_pause_resume_cancel_retry(dash):
    run = dash.flow.new_run()
    assert dash.post(f"/api/runs/{run.id}/pause")[0] == 200
    assert dash.spawned == []  # pausing starts nothing
    status, _, body = dash.post(f"/api/runs/{run.id}/pause")
    assert status == 409 and "already paused" in payload(body)["error"]
    assert dash.post(f"/api/runs/{run.id}/resume")[0] == 200
    run.refresh()
    assert [e["by"] for e in dash.flow.events(run, "run_resumed")] == ["dashboard"]
    assert [s["command"][-2:] for s in dash.spawned] == [["resume", run.id]]

    status, _, body = dash.post(f"/api/runs/{run.id}/retry")
    assert status == 409 and "no failed step" in payload(body)["error"]
    run.emit("step_started", key="build/x", phase="build")
    lock = run.lock()
    assert lock.acquire()
    try:
        status, _, body = dash.post(f"/api/runs/{run.id}/retry", {"key": "build/x"})
        assert status == 409 and "runner is working" in payload(body)["error"]
    finally:
        lock.release()

    assert dash.post(f"/api/runs/{run.id}/cancel")[0] == 200
    run.refresh()
    assert run.state["status"] == "cancelled" and len(dash.spawned) == 1
    assert dash.post(f"/api/runs/{run.id}/cancel")[0] == 409


def test_action_on_a_live_runner_does_not_spawn_a_second(dash):
    run = ready(dash.flow)
    lock = run.lock()
    assert lock.acquire()
    try:
        status, _, body = dash.post(f"/api/runs/{run.id}/resume")
        assert status == 200 and payload(body)["resumed"] is False
    finally:
        lock.release()
    assert dash.spawned == []


# --------------------------------------------------------------------------- the actions a run's state allows


def _paused(flow):
    run = flow.new_run()
    pause(run, "me")
    return run


def _questions(flow):
    run = flow.new_run()
    questions = [hitl.Question("q1", "How long?", [hitl.Option("A", "1h", True)], [], "spec_gap")]
    hitl.raise_hitl(flow.cfg, run, "needs_human", "define/draft", "Needs a decision", questions)
    return run


def _stalled(flow):
    run = flow.new_run()
    run.emit("phase_started", phase="build")
    run.emit("step_started", key="build/x", phase="build")
    return run


def _failed_step(flow):
    run = _stalled(flow)
    run.emit("step_finished", key="build/x", status="failed")
    return run


def _cancelled(flow):
    run = flow.new_run()
    cancel(run, "t", cfg=flow.cfg)
    return run


def _stop(kind: str):
    def build(flow):
        run = flow.new_run()
        run.emit("phase_started", phase="build")
        hitl.raise_hitl(flow.cfg, run, kind, "build/x", f"stopped: {kind}")
        return run
    return build


BUILDERS = {
    "paused": _paused,
    "ready": lambda flow: ready(flow),
    "ready_with_work": lambda flow: with_work(flow, ready(flow)),
    "phase_approval": lambda flow: awaiting(flow),
    "questions": _questions,
    "environment": _stop(hitl.ENVIRONMENT),
    "preflight_failed": _stop("preflight_failed"),
    "blocked_repeat": _stop("blocked_repeat"),
    "stalled": _stalled,
    "failed_step": _failed_step,
    "failed_step_live": _failed_step,  # the same run, with a runner holding its lock (taken in the test)
    "done": lambda flow: finished_run(flow),
    "cancelled": _cancelled,
}
LIVE = ("failed_step_live",)
NOTES = ("blocked_repeat",)  # a stop that only a note approves
PAUSE_CANCEL = ["pause", "cancel"]


def body_for(build: str, action: str) -> dict:
    """What the action needs to succeed on this state: a phase approval takes no note, a repeated block needs one."""
    if action == "approve":
        return {"note": "go"} if build in NOTES else {}
    return {"reject": {"note": "redo"}, "answer": {"answers": {"q1": "A"}}}.get(action, {})


@pytest.mark.parametrize("build, listed", [
    ("paused",           ["resume", "cancel"]),
    ("ready",            ["resume", "cancel"]),               # no work in the checkout -> no done
    ("ready_with_work",  ["resume", "done", "cancel"]),
    ("phase_approval",   ["approve", "reject", "pause", "cancel"]),
    ("questions",        ["answer", "reject", "pause", "cancel"]),
    ("environment",      ["approve", "pause", "cancel"]),     # hitl.reject refuses an environment stop
    ("preflight_failed", ["approve", "pause", "cancel"]),     # and a preflight one
    ("blocked_repeat",   ["approve", "reject", "pause", "cancel"]),  # approve needs a note: body_for gives it one
    ("stalled",          ["resume", "pause", "cancel"]),
    ("failed_step",      ["resume", "pause", "retry", "cancel"]),
    ("failed_step_live", PAUSE_CANCEL),                       # a live runner: nothing to retry or resume
    ("done",             []),
    ("cancelled",        []),
])
@pytest.mark.parametrize("action", RUN_ACTIONS)
def test_listed_actions_succeed_and_unlisted_ones_are_refused(dash, build, listed, action):
    run = BUILDERS[build](dash.flow)
    lock = run.lock() if build in LIVE else None
    if lock is not None:
        assert lock.acquire()
    try:
        assert available_actions(run) == listed
        status, _, body = dash.post(f"/api/runs/{run.id}/{action}", body_for(build, action))
    finally:
        if lock is not None:
            lock.release()
    if action in listed:
        assert status == 200, (status, payload(body))
    elif action == "resume" and build not in ("done", "cancelled"):
        # the one wrong press the routes allow: resume answers 200 and starts a runner (or sees one live). Hiding
        # the button is what prevents it
        assert status == 200, (status, payload(body))
    else:
        assert status == 409, (status, payload(body))


def test_a_repeated_block_lists_approve_though_only_an_approval_with_a_note_goes_through(dash):
    """The known over-listing: `available_actions` cannot know whether the person will write a note."""
    run = BUILDERS["blocked_repeat"](dash.flow)
    assert "approve" in available_actions(run)
    status, _, body = dash.post(f"/api/runs/{run.id}/approve", {})
    assert status == 409 and "with nothing changed" in payload(body)["error"]
    assert dash.post(f"/api/runs/{run.id}/approve", {"note": "fixed the cause"})[0] == 200


def test_a_retro_run_lists_only_pause_and_cancel(dash):
    run = Run.create(dash.flow.cfg, dash.flow.root, RETRO_PIPELINE, {}, engine_pipeline=True)
    run.emit("phase_started", phase="retro")
    assert available_actions(run) == PAUSE_CANCEL
    for action in ("approve", "reject", "answer", "resume", "done", "retry"):
        status, _, body = dash.post(f"/api/runs/{run.id}/{action}", body_for("retro", action))
        assert status == 409, (action, status, payload(body))
    assert dash.spawned == []


def test_a_retro_run_that_waits_still_lists_only_pause_and_cancel(dash):
    run = Run.create(dash.flow.cfg, dash.flow.root, RETRO_PIPELINE, {}, engine_pipeline=True)
    run.emit("phase_started", phase="retro")
    questions = [hitl.Question("q1", "Which?", [hitl.Option("A", "this", True)], [], "other")]
    hitl.raise_hitl(dash.flow.cfg, run, "needs_human", "retro/draft", "Stopped", questions)
    assert available_actions(run) == PAUSE_CANCEL
    for action in ("approve", "reject", "answer", "resume", "retry"):
        status, _, body = dash.post(f"/api/runs/{run.id}/{action}", body_for("retro", action))
        assert status == 409, (action, status, payload(body))


# --------------------------------------------------------------------------- live updates


def test_sse_streams_new_events(dash):
    run = awaiting(dash.flow)
    conn = http.client.HTTPConnection("127.0.0.1", dash.port, timeout=5)
    try:
        conn.request("GET", f"/api/events?token={TOKEN}")
        response = conn.getresponse()
        assert response.status == 200 and response.getheader("Content-Type").startswith("text/event-stream")
        assert response.fp.readline() == b": connected\n"  # the stream's starting point is fixed once this arrives
        run.emit("run_paused", by="sse-test")
        deadline = time.monotonic() + 2.0
        seen = None
        while time.monotonic() < deadline and seen is None:
            line = response.fp.readline().decode("utf-8")
            if line.startswith("data: "):
                message = json.loads(line[len("data: "):])
                if message["event"]["type"] == "run_paused":
                    seen = message
        assert seen is not None and time.monotonic() <= deadline
        assert seen["run_id"] == run.id and seen["event"]["by"] == "sse-test"
    finally:
        conn.close()


def test_a_quiet_stream_coalesces_agent_turns(dash):
    run = awaiting(dash.flow)
    run.emit("agent_started", agent="claude", pid=os.getpid(), model="opus", effort="high", step="plan/plan", role="plan")
    run.emit("agent_session", session_id="s-1", model="claude-opus-5-5", key="plan/plan", pid=os.getpid())
    conn = http.client.HTTPConnection("127.0.0.1", dash.port, timeout=5)
    try:
        conn.request("GET", f"/api/events?quiet=1&token={TOKEN}")
        response = conn.getresponse()
        assert response.fp.readline() == b": connected\n"
        for i in range(10):
            run.emit("agent_turn", session_id="s-1", key="plan/plan", tokens=100 + i, context_pct=10 + i,
                     cost=0.01 * i, last_tool=f"Edit-{i}")
        run.emit("run_paused", by="t")
        seen = []

        def read() -> None:  # the stream goes quiet after the last message, so a blocked read must not stall the test
            try:
                while line := response.fp.readline().decode("utf-8"):
                    if line.startswith("data: "):
                        seen.append(json.loads(line[6:])["event"])
            except (OSError, ValueError):
                pass

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        time.sleep(3.5)
        snapshot = list(seen)
    finally:
        conn.close()
    seen = snapshot
    types = [e["type"] for e in seen]
    assert "agent_turn" not in types and "run_paused" in types
    activity = [e for e in seen if e["type"] == "agent_activity"]
    assert 1 <= len(activity) <= 2 and activity[-1]["last_tool"] == "Edit-9"


def test_sse_reports_runs_created_after_connecting(dash):
    conn = http.client.HTTPConnection("127.0.0.1", dash.port, timeout=5)
    try:
        conn.request("GET", "/api/events", headers={"X-Agentflow-Token": TOKEN})
        response = conn.getresponse()
        assert response.fp.readline() == b": connected\n"
        run = dash.flow.new_run({"module": "later"})
        deadline = time.monotonic() + 2.0
        types: list[str] = []
        while time.monotonic() < deadline and "run_started" not in types:
            line = response.fp.readline().decode("utf-8")
            if line.startswith("data: "):
                message = json.loads(line[len("data: "):])
                if message["run_id"] == run.id:
                    types.append(message["event"]["type"])
        assert "run_started" in types
    finally:
        conn.close()


def test_cancelled_run_cannot_be_approved_from_the_dashboard(dash):
    from agentflow.run_control import cancel

    run = awaiting(dash.flow)
    cancel(run, by="me", cfg=dash.flow.cfg)
    before = run.events.last_seq()
    for action, body in (("approve", {"target": "plan"}), ("approve", {}), ("reject", {"note": "muộn rồi"})):
        status, _, raw = dash.post(f"/api/runs/{run.id}/{action}", body)
        assert status == 409 and "cancelled" in payload(raw)["error"], action
    assert run.events.last_seq() == before and dash.spawned == []


# --------------------------------------------------------------------------- the page and reloads


def test_tokenless_page_is_a_shell_without_run_data(dash):
    run = awaiting(dash.flow, "bi-mat")
    hitl.reject(run, "plan", "me", "ghi chú riêng tư")
    status, headers, body = dash.get("/", token=None)
    page = body.decode("utf-8")
    assert status == 200 and headers["content-type"].startswith("text/html")
    assert run.id not in page and "bi-mat" not in page and "ghi chú" not in page
    assert "agentflow dashboard" in page_source()  # it tells a visitor without a token where the URL comes from
    assert "default-src 'none'" in headers["content-security-policy"]
    assert headers["referrer-policy"] == "no-referrer" and headers["cache-control"] == "no-store"
    assert dash.get("/api/board", token=None)[0] == 403  # the data stays behind the token
    assert dash.get(f"/api/runs/{run.id}", token=None)[0] == 403
    assert dash.get("/index.html", token=None)[0] == 403  # only "/" itself is open
    assert dash.get("/", token=None, headers={"Host": "evil.example"})[0] == 403  # Host still checked


def test_dashboard_url_carries_the_token_in_the_fragment():
    from agentflow.dashboard import dashboard_url

    url = dashboard_url(7788, TOKEN)
    assert url == f"http://127.0.0.1:7788/#token={TOKEN}"
    assert "?" not in url  # never in a query string: fragments are not sent to the server or its log


def test_page_script_bootstraps_the_token_from_the_fragment_and_keeps_it_for_reloads():
    source = page_source()
    boot = source[source.index("const TOKEN"):source.index("const ui")]
    assert "location.hash" in boot  # the first visit reads #token=...
    assert 'sessionStorage.getItem("agentflow-token")' in boot  # a reload falls back to the stored token
    assert 'sessionStorage.setItem("agentflow-token"' in boot
    assert "history.replaceState" in boot and "location.search" not in boot  # the fragment is removed; no query token
    assert "open the URL printed by `agentflow dashboard`" in source  # what a tokenless visit shows


def test_run_drawer_never_hands_null_to_replace_children():
    """The DOM prints a null argument as the text "null"; only `el()` skips null children."""
    source = page_source()
    block = source[source.index("peek.replaceChildren(\n"):source.index("function closePeek")]
    assert not re.search(r":\s*null\b", block)


def test_a_route_hash_survives_the_token_boot():
    source = page_source()
    boot = source[source.index("const TOKEN"):source.index("const ui")]
    assert "if (match)" in boot and "if (location.hash)" not in boot   # only #token= is stripped


def test_hidden_wins_over_an_author_display_rule():
    """`.run-banner { display: flex }` (and `.set-grid`'s grid) would beat the user agent's `[hidden]` rule."""
    page = (AGENTFLOW / "dashboard.html").read_text(encoding="utf-8")
    assert "[hidden] { display: none !important; }" in page


def test_runs_have_hash_routes():
    source = page_source()
    for needle in ('"#/run/"', "hashchange", "function parseRoute", "function routeHash",
                   'RUN_TABS = ["overview", "spec", "plan", "activity", "gates", "files"]'):
        assert needle in source, needle


def test_run_page_and_peek_buttons_come_only_from_the_actions_list():
    run_js = (UI_DIR / "run.js").read_text(encoding="utf-8")
    drawer_js = (UI_DIR / "drawer.js").read_text(encoding="utf-8")
    block = run_js[run_js.index("const ACTION_BUTTONS"):run_js.index("\n};", run_js.index("const ACTION_BUTTONS"))]
    outside = run_js.replace(block, "") + drawer_js
    assert not re.search(r'act\([^)]*"(pause|resume|retry|cancel|done)"', outside)
    assert "detail.actions" in run_js and "detail.actions" in drawer_js


def test_an_unknown_run_says_so_and_links_back():
    assert "No run " in (UI_DIR / "run.js").read_text(encoding="utf-8")


def test_the_drawer_is_a_peek_with_an_open_run_button():
    drawer_js = (UI_DIR / "drawer.js").read_text(encoding="utf-8")
    assert "Open run" in drawer_js and "function openPeek" in drawer_js and "openDrawer" not in page_source()


def test_the_page_listens_quietly_and_refreshes_in_buckets():
    source = page_source()
    assert "/api/events?quiet=1&token=" in source
    on_message = source[source.index("stream.onmessage"):source.index("\n", source.index("stream.onmessage"))]
    assert "onStreamMessage" in on_message and "scheduleRefresh" not in source
    assert "agent_activity" in source and "patchActivity(" in source and "board_refresh_s" in source


def test_live_regions_patch_by_key():
    for name in ("board.js", "run.js", "drawer.js"):
        assert "patchChildren(" in (UI_DIR / name).read_text(encoding="utf-8"), name
    core = (UI_DIR / "core.js").read_text(encoding="utf-8")
    assert "function patchChildren" in core and "function throttle" in core and "data-sig" in core


def test_a_replaced_section_keeps_its_scroll():
    """A live run's log in a `pre` (max-height, overflow: auto) must not jump to the top when its section is rebuilt."""
    core = (UI_DIR / "core.js").read_text(encoding="utf-8")
    region = core[core.index("function patchChildren"):core.index("const entry =")]
    assert "scrollTop" in region and "scrollLeft" in region and '"pre, .scroll"' in region
    # the offsets are applied after the placement loop: a detached element ignores scrollTop
    call = "for (const [node, offsets] of scrolls) keepScroll(node, offsets);"
    assert call in region, "the offsets are never applied"
    assert region.index(call) > region.index("parent.insertBefore(node, cursor)")


def test_a_reconnect_marks_everything_dirty():
    main = (UI_DIR / "main.js").read_text(encoding="utf-8")
    onopen = main[main.index("stream.onopen"):main.index("stream.onmessage")]
    assert "dirty.board = true" in onopen and "dirty.agents = true" in onopen and "dirty.runs.add" in onopen


def test_sse_keeps_an_event_written_as_soon_as_the_headers_arrive(dash, monkeypatch):
    """A client acting on the 200 at once (the race): its event must still be streamed."""
    from agentflow import dashboard

    run = awaiting(dash.flow)
    real_end_headers = dashboard._Handler.end_headers

    def end_headers_then_act(handler):
        real_end_headers(handler)
        if handler.path.startswith("/api/events"):
            run.emit("run_paused", by="racer")

    monkeypatch.setattr(dashboard._Handler, "end_headers", end_headers_then_act)
    conn = http.client.HTTPConnection("127.0.0.1", dash.port, timeout=5)
    try:
        conn.request("GET", "/api/events", headers={"X-Agentflow-Token": TOKEN})
        response = conn.getresponse()
        deadline = time.monotonic() + 2.0
        seen = None
        while time.monotonic() < deadline and seen is None:
            line = response.fp.readline().decode("utf-8")
            if line.startswith("data: "):
                message = json.loads(line[len("data: "):])
                if message["event"]["type"] == "run_paused":
                    seen = message
        assert seen is not None and seen["event"]["by"] == "racer"
    finally:
        conn.close()


# --------------------------------------------------------------------------- the workflow


def test_workflow_route_needs_the_token_and_shows_every_phase(dash):
    assert dash.get("/api/workflow", token=None)[0] == 403
    status, _, body = dash.get("/api/workflow")
    view = payload(body)
    assert status == 200 and view["lanes"] == ["feature", "bug", "eval", "design"]
    assert [p["name"] for p in view["phases"]] == ["define", "plan", "build", "verify", "review", "ship"]
    assert all(step["lanes"] for phase in view["phases"] for step in phase["steps"])


def test_board_holds_every_lane_and_a_lane_narrows_it(dash):
    feature = awaiting(dash.flow)
    bug = Run.create(dash.flow.cfg, dash.flow.root, "bug", {"task": "lỗi"})
    data = payload(dash.get("/api/board")[2])
    assert data["columns"] == ["Backlog", "define", "plan", "build", "verify", "review", "ship", "Done"]
    assert {c["run_id"]: c["lane"] for c in data["cards"]} == {feature.id: "feature", bug.id: "bug"}
    narrowed = payload(dash.get("/api/board?lane=bug")[2])
    assert [c["run_id"] for c in narrowed["cards"]] == [bug.id] and narrowed["lane"] == "bug"


def test_run_detail_shows_the_runs_walk_of_the_workflow(dash):
    run = awaiting(dash.flow)
    detail = payload(dash.get(f"/api/runs/{run.id}")[2])
    phases = {p["name"]: p for p in detail["workflow"]}
    assert list(phases) == ["define", "plan", "build", "verify", "review", "ship"]
    assert phases["plan"]["status"] == "awaiting_approval"
    assert [(s["id"], s["skill"]) for s in phases["plan"]["steps"]] == [("plan", "writing-plans")]
    assert phases["verify"]["status"] == "not reached"


# --------------------------------------------------------------------------- a phase that needs no human


def test_approving_plan_starts_build_and_approving_define_rests(dash, flow_repo):
    dash.server.cfg = flow_repo.configure(autopilot=True)
    plan = awaiting(flow_repo, "crm", "plan")
    status, _, body = dash.post(f"/api/runs/{plan.id}/approve", {"target": "plan"})
    assert status == 200 and json.loads(body)["resumed"] is True
    assert plan.refresh()["status"] != "ready"
    define = awaiting(flow_repo, "ops", "define")
    status, _, body = dash.post(f"/api/runs/{define.id}/approve", {"target": "define"})
    assert status == 200 and define.refresh()["status"] == "ready"  # `plan` still has its human


def test_a_lane_with_no_plan_steps_goes_straight_on_after_define(dash, flow_repo):
    dash.server.cfg = flow_repo.configure(autopilot=True)
    run = Run.create(flow_repo.cfg, flow_repo.root, "bug", {"task": "lỗi"})
    run.emit("phase_started", phase="define")
    run.emit("phase_approval_raised", phase="define", round=0, summary="Phase define is done")
    assert dash.post(f"/api/runs/{run.id}/approve", {"target": "define"})[0] == 200
    assert run.refresh()["status"] != "ready"  # the next phase with work for a bug is `build`


def test_a_card_says_when_the_agent_is_on_it_and_when_ci_is(flow_repo):
    run = flow_repo.new_run({"module": "crm"})
    assert _badge({**run.state, "resolving": {"kind": "blocked"}}, "running", "build") == "agent fixing: blocked"
    assert _badge({**run.state, "ci": {"url": "x"}}, "running", "ship") == "waiting for CI"


# --------------------------------------------------------------------------- the Backlog (starting work from the page)

BACKLOG_MAP = """# Capability map

| Module id | Responsibility | Depends on | Spec |
|---|---|---|---|
| `a` | base | — | SPEC-a |
| `b` | bee | a | SPEC-b |
| `c` | sea | — | SPEC-c |
"""


class FakeSpawn:
    """Stands in for `spawn_run`: records each call and creates the run a real start would."""

    def __init__(self, flow) -> None:
        self.flow = flow
        self.calls: list[dict] = []
        self.error: str | None = None

    def __call__(self, repo, lane, params, **kw):
        from agentflow.module_queue import StartFailed

        self.calls.append({"lane": lane, "params": dict(params), **kw})
        if self.error:
            raise StartFailed(self.error)
        run = Run.create(self.flow.cfg, repo, lane, params)
        run.emit("run_updated", branch=f"flow/{lane}/{params.get('module') or params.get('issue')}")
        return run.id


@pytest.fixture
def bdash(flow_repo):
    (flow_repo.root / "docs/specs").mkdir(parents=True, exist_ok=True)
    (flow_repo.root / "docs/specs/CAPABILITY-MAP.md").write_text(BACKLOG_MAP, encoding="utf-8")
    run_git(flow_repo.root, "add", "-A")
    run_git(flow_repo.root, "commit", "-q", "-m", "map")
    spawn = FakeSpawn(flow_repo)
    server = make_server(flow_repo.root, flow_repo.cfg, "127.0.0.1", 0, TOKEN, spawn_run=spawn)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    dash = Dash(server, server.server_address[1], flow_repo, [])
    dash.fake = spawn
    yield dash
    server.close()
    thread.join(timeout=5)


def settle(dash) -> None:
    """Wait for every start the server began (they run on worker threads)."""
    for thread in list(dash.server.starts):
        thread.join(timeout=10)


def backlog_of(dash, lane: str | None = None) -> dict:
    path = "/api/board" + (f"?lane={lane}" if lane else "")
    return {c["key"]: c for c in payload(dash.get(path)[2])["backlog"]}


def test_the_board_carries_the_backlog_and_the_new_task_form(bdash):
    data = payload(bdash.get("/api/board")[2])
    cards = {c["key"]: c for c in data["backlog"]}
    assert sorted(cards) == ["module:a", "module:b", "module:c"]
    assert cards["module:a"]["status"] == "parallel" and cards["module:b"]["status"] == "blocked"
    assert cards["module:a"]["first_phase"] == "define"
    assert data["new_task"]["bug"] == ["issue", "task", "after"]
    assert data["lane_phases"]["bug"][0] == "define" and "sync_note" in data and "after_choices" in data
    assert data["modules"] == ["a", "b", "c"]  # what a new module may depend on
    assert "lane_labels" in data  # the form's plain words for each kind of work


def test_each_lane_sees_its_own_backlog_cards(bdash):
    code, _, body = bdash.post("/api/backlog", {"lane": "bug", "fields": {"issue": "7"}})
    assert code == 200 and payload(body)["key"].startswith("card:")
    assert sorted(k for k in backlog_of(bdash, "bug")) == [payload(body)["key"]]
    assert sorted(backlog_of(bdash, "feature")) == ["module:a", "module:b", "module:c"]
    assert len(backlog_of(bdash)) == 4


def test_a_bad_card_is_a_400_and_a_duplicate_a_409(bdash):
    assert bdash.post("/api/backlog", {"lane": "bug", "fields": {"issue": "x; rm"}})[0] == 400
    assert bdash.post("/api/backlog", {"lane": "nope", "fields": {"task": "t"}})[0] == 400
    assert bdash.post("/api/backlog", {"lane": "bug", "fields": "t"})[0] == 400
    assert bdash.post("/api/backlog", {"lane": "bug", "fields": {"issue": "7"}})[0] == 200
    code, _, body = bdash.post("/api/backlog", {"lane": "bug", "fields": {"issue": "7"}})
    assert code == 409 and "7" in payload(body)["error"]


def test_a_refused_request_logs_its_reason_with_its_status(bdash, capsys):
    assert bdash.post("/api/backlog", {"lane": "bug", "fields": {"issue": "x; rm"}})[0] == 400
    err = capsys.readouterr().err
    assert "dashboard: POST /api/backlog -> 400: --issue takes an issue number such as 42" in err
    assert TOKEN not in err


def test_a_reason_is_logged_once_and_never_against_the_next_request_on_the_connection(bdash, capsys):
    conn = http.client.HTTPConnection("127.0.0.1", bdash.port, timeout=5)  # one connection, two requests
    bad = json.dumps({"lane": "bug", "fields": {"issue": "x; rm"}})
    try:
        statuses = []
        for headers in ({"X-Agentflow-Token": TOKEN}, {}):  # the second has no token: a 403 with no reason
            conn.request("POST", "/api/backlog", body=bad, headers={"Content-Type": "application/json", **headers})
            response = conn.getresponse()
            response.read()
            statuses.append(response.status)
        assert statuses == [400, 403]
    finally:
        conn.close()
    lines = [line for line in capsys.readouterr().err.splitlines() if "/api/backlog ->" in line]
    assert lines[0].startswith("dashboard: POST /api/backlog -> 400: --issue takes")
    assert lines[1] == "dashboard: POST /api/backlog -> 403"


def test_starting_a_ready_module_answers_202_and_the_run_appears(bdash):
    code, _, body = bdash.post("/api/backlog/start", {"module": "c"})
    assert code == 202 and payload(body)["starting"] is True
    settle(bdash)
    assert bdash.fake.calls[0]["lane"] == "feature" and bdash.fake.calls[0]["params"] == {"module": "c"}
    assert "module:c" not in backlog_of(bdash)  # it is a run now
    assert [c["module"] for c in payload(bdash.get("/api/board")[2])["cards"]] == ["c"]


def test_starting_a_card_by_item_key(bdash):
    key = payload(bdash.post("/api/backlog", {"lane": "bug", "fields": {"task": "Traceback: x"}})[2])["key"]
    assert bdash.post("/api/backlog/start", {"item": key})[0] == 202
    settle(bdash)
    assert bdash.fake.calls[0]["params"] == {"task": "Traceback: x"}


def test_a_blocked_module_cannot_be_started_even_by_a_direct_post(bdash):
    code, _, body = bdash.post("/api/backlog/start", {"module": "b"})
    assert code == 409 and "a" in payload(body)["error"]
    settle(bdash)
    assert bdash.fake.calls == []


def test_a_start_that_fails_keeps_the_card_with_its_reason(bdash):
    bdash.fake.error = "error: gh is down"
    assert bdash.post("/api/backlog/start", {"module": "c"})[0] == 202
    settle(bdash)
    card = backlog_of(bdash)["module:c"]
    assert card["failure"] == "error: gh is down"
    bdash.fake.error = None
    assert bdash.post("/api/backlog/start", {"module": "c"})[0] == 202
    settle(bdash)
    assert len(bdash.fake.calls) == 2 and "module:c" not in backlog_of(bdash)


def test_start_needs_a_module_or_an_item(bdash):
    assert bdash.post("/api/backlog/start", {})[0] == 400
    assert bdash.post("/api/backlog/start", {"item": "card:zzzz"})[0] == 409
    assert bdash.post("/api/backlog/start", {"item": "run:x"})[0] == 400
    assert bdash.post("/api/backlog/start", {"module": "nope"})[0] == 409


def test_remove_drops_a_card_unless_another_runs_after_it(bdash):
    first = payload(bdash.post("/api/backlog", {"lane": "bug", "fields": {"issue": "1"}})[2])
    second = payload(bdash.post("/api/backlog", {"lane": "bug", "fields": {"issue": "2"}, "after": [first["key"]]})[2])
    code, _, body = bdash.post("/api/backlog/remove", {"item": first["key"]})
    assert code == 409 and "2" in payload(body)["error"]
    assert bdash.post("/api/backlog/remove", {"item": second["key"]})[0] == 200
    assert bdash.post("/api/backlog/remove", {"item": first["key"]})[0] == 200
    assert bdash.post("/api/backlog/remove", {"item": "module:a"})[0] == 409
    assert not [k for k in backlog_of(bdash) if k.startswith("card:")]


@pytest.mark.parametrize("path, body", [
    ("/api/backlog", {"lane": "bug", "fields": {"issue": "7"}}),
    ("/api/backlog/start", {"module": "c"}),
    ("/api/backlog/remove", {"item": "card:x"}),
])
def test_backlog_routes_need_the_token_and_our_origin(bdash, path, body):
    assert bdash.post(path, body, token=None)[0] == 403
    assert bdash.post(path, body, headers={"Origin": "https://evil.example"})[0] == 403
    assert bdash.post(path, body, headers={"Host": "evil.example"})[0] == 403
    assert bdash.fake.calls == [] and backlog_of(bdash).keys() == {"module:a", "module:b", "module:c"}


def test_backlog_bodies_must_be_json_objects(bdash):
    assert bdash.request("POST", "/api/backlog/start", body=b'{"module": "c"}', headers={"Content-Type": "text/plain"})[0] == 415
    assert bdash.request("POST", "/api/backlog/start", body=b"[1]")[0] == 400
    assert bdash.request("POST", "/api/backlog/other", body=b"{}")[0] == 404
    assert bdash.get("/api/backlog/start")[0] == 404  # GET does nothing here


def test_backlog_events_join_the_live_stream(bdash):
    conn = http.client.HTTPConnection("127.0.0.1", bdash.port, timeout=5)
    try:
        conn.request("GET", "/api/events", headers={"X-Agentflow-Token": TOKEN})
        response = conn.getresponse()
        assert response.fp.readline() == b": connected\n"
        assert bdash.post("/api/backlog", {"lane": "bug", "fields": {"issue": "9"}})[0] == 200
        deadline = time.monotonic() + 3.0
        seen = None
        while time.monotonic() < deadline and seen is None:
            line = response.fp.readline().decode("utf-8")
            if line.startswith("data: "):
                message = json.loads(line[len("data: "):])
                if message["event"]["type"] == "item_added":
                    seen = message
        assert seen is not None and seen["run_id"] == "backlog"
    finally:
        conn.close()


def test_the_server_settles_interrupted_starts_when_it_comes_up(bdash):
    from agentflow import backlog
    from agentflow.dashboard import prepare

    backlog.begin_start(bdash.flow.cfg, bdash.flow.root, "module:c")  # the dashboard died after recording this
    assert backlog_of(bdash)["module:c"]["status"] == "starting"
    prepare(bdash.server)
    card = backlog_of(bdash)["module:c"]
    assert "interrupted" in card["failure"] and card["status"] != "starting"
    assert bdash.fake.calls == []


def test_the_refresh_round_updates_the_cards_and_starts_nothing(bdash):
    from agentflow.dashboard import refresh_once

    refresh_once(bdash.server)
    assert (runs_root(bdash.flow.root) / "queue.json").is_file()
    assert bdash.fake.calls == []


def test_a_failing_refresh_round_does_not_stop_the_loop(bdash, monkeypatch, capsys):
    from agentflow import backlog
    from agentflow.dashboard import refresh_once

    monkeypatch.setattr(backlog, "refresh", lambda cfg, repo: (_ for _ in ()).throw(OSError("disk full")))
    refresh_once(bdash.server)
    assert "disk full" in capsys.readouterr().err


@pytest.fixture
def idash(bdash, monkeypatch, tmp_path):
    """`bdash` with the fake `gh` on PATH, `issues.sync_label: agentflow`, and two labelled issues listed."""
    gh_bin = Path(__file__).parent / "bin"
    monkeypatch.setenv("PATH", f"{gh_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.log"))
    issues = [{"number": 12, "title": "<img src=x onerror=alert(1)>", "labels": [{"name": "agentflow"}, {"name": "bug"}],
               "url": "https://github.com/example/repo/issues/12",
                "blockedBy": {"nodes": [], "totalCount": 0}, "blocking": {"nodes": [], "totalCount": 0}},
              {"number": 14, "title": "Taken", "labels": [{"name": "agentflow"}, {"name": "agentflow:in-progress"}],
               "url": "https://github.com/example/repo/issues/14",
                "blockedBy": {"nodes": [], "totalCount": 0}, "blocking": {"nodes": [], "totalCount": 0}}]
    monkeypatch.setenv("FAKE_GH_ISSUE_LIST", json.dumps(issues))
    bdash.flow.configure({"issues": {"sync_label": "agentflow"}})
    bdash.server.cfg = bdash.flow.cfg
    from agentflow.dashboard import refresh_once

    refresh_once(bdash.server)
    return bdash


def test_the_board_carries_synced_issues_and_their_note(idash):
    data = payload(idash.get("/api/board")[2])
    cards = {c["key"]: c for c in data["backlog"]}
    assert cards["issue:12"]["label"] == "#12 · <img src=x onerror=alert(1)>"  # shown through textContent
    assert cards["issue:12"]["lane"] == "bug" and cards["issue:14"]["status"] == "taken"
    assert data["issues_note"] == ""


def test_a_synced_issue_starts_but_a_taken_or_bad_one_does_not_and_none_can_be_removed(idash):
    code, _, body = idash.post("/api/backlog/start", {"item": "issue:12"})
    assert code == 202, body
    settle(idash)
    assert idash.fake.calls[-1]["params"] == {"issue": "12"}
    code, _, body = idash.post("/api/backlog/start", {"item": "issue:14"})
    assert code == 409 and "in-progress label" in payload(body)["error"]
    assert idash.post("/api/backlog/start", {"item": "issue:abc"})[0] == 400
    code, _, body = idash.post("/api/backlog/remove", {"item": "issue:14"})
    assert code == 409 and "loses the 'agentflow' label" in payload(body)["error"]


@pytest.fixture
def ddash(bdash, monkeypatch, tmp_path):
    """`bdash` with the fake `gh` on PATH and two labelled issues: #12 is blocked by #14, which blocks #12."""
    gh_bin = Path(__file__).parent / "bin"
    monkeypatch.setenv("PATH", f"{gh_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.log"))

    def node(number):
        return {"id": f"I_{number}", "number": number, "state": "OPEN", "title": "t",
                "url": f"https://github.com/example/repo/issues/{number}"}

    def refs(*nodes):
        return {"nodes": list(nodes), "totalCount": len(nodes)}

    issues = [{"number": 12, "title": "Blocked", "labels": [{"name": "agentflow"}],
               "url": "https://github.com/example/repo/issues/12", "blockedBy": refs(node(14)), "blocking": refs()},
              {"number": 14, "title": "Blocker", "labels": [{"name": "agentflow"}],
               "url": "https://github.com/example/repo/issues/14", "blockedBy": refs(), "blocking": refs(node(12))}]
    monkeypatch.setenv("FAKE_GH_ISSUE_LIST", json.dumps(issues))
    bdash.flow.configure({"issues": {"sync_label": "agentflow"}})
    bdash.server.cfg = bdash.flow.cfg
    from agentflow.dashboard import refresh_once

    refresh_once(bdash.server)
    return bdash


def test_the_board_carries_blockers_and_blocks(ddash):
    cards = {c["key"]: c for c in payload(ddash.get("/api/board")[2])["backlog"]}
    assert cards["issue:12"]["status"] == "blocked"
    assert cards["issue:12"]["blocked_by"][0]["text"] == "issue #14 (in Backlog)"
    assert cards["issue:14"]["blocks"] == [{"key": "issue:12", "label": "#12"}]


def test_starting_a_blocked_issue_is_a_409(ddash):
    code, _, body = ddash.post("/api/backlog/start", {"item": "issue:12"})
    assert code == 409 and "runs after issue #14" in payload(body)["error"]
    assert ddash.fake.calls == []


def test_the_page_renders_the_blocks_line():
    page = page_source()
    assert 'text: "Blocks: " + names(item.blocks)' in page


def test_a_pack_without_a_backlog_block_still_syncs_its_issues(idash, monkeypatch):
    from agentflow import backlog
    from agentflow.dashboard import refresh_once

    monkeypatch.setattr(backlog, "refresh", lambda cfg, repo: pytest.fail("no backlog block: no git refresh"))
    idash.flow.configure({"issues": {"sync_label": "agentflow"}, "dashboard": {"backlog": None}})
    idash.server.cfg = idash.flow.cfg
    monkeypatch.setenv("FAKE_GH_ISSUE_LIST", "[]")
    refresh_once(idash.server)
    assert backlog._load_state(idash.flow.root)["issues"] == []


def test_the_page_names_no_lane_phase_or_path(bdash):
    """The page is framework code: lanes, phases and the form fields all come from the board data."""
    # the two tab lists and `PLAN_TAB` name the artifact tabs (`plan` is a tab id there, as `define`'s `spec` is); nothing else may
    source = "\n".join(line for line in page_source().splitlines() if "RUN_TABS = [" not in line and "ARTIFACT_KINDS = [" not in line
                        and "PLAN_TAB = " not in line)
    cfg = bdash.flow.cfg
    for word in [*cfg.lanes, "CAPABILITY-MAP", "docs/", "origin/", "flow/"]:
        assert not re.search(rf"(?<![A-Za-z-]){re.escape(word)}(?![A-Za-z-])", source), word
    # a phase word may appear in prose, but never as a string the script compares against
    for phase in cfg.phases:
        assert f'"{phase}"' not in source and f"'{phase}'" not in source, phase


def test_the_page_shows_one_workflow_with_no_lane_filter():
    source = page_source()
    for gone in ('id="lane"', "ui.lane", 'lane: ""', "All lanes", "inLane", '"?lane="', '"Lane · phase › step"'):
        assert gone not in source, gone
    for card in ("text: card.lane", "text: item.lane"):
        assert card not in source, card  # a card names its work, not its lane
    assert "Phase › step" in source and "text: r.place" in source


def test_a_workflow_step_is_badged_only_when_it_runs_for_one_kind_of_work():
    source = page_source()
    block = source[source.index("function onlyFor"):source.index("function renderWorkflow")]
    assert "step.lanes.length !== 1" in block  # a step for two lanes or more carries no badge
    assert 'workLabel(step.lanes[0]) + " only"' in block
    assert "parent" in block  # a nested step doesn't repeat the badge of the step around it


def test_a_step_with_no_skill_is_labelled_by_what_it_is():
    source = page_source()
    start = source.index("function stepSkills")
    block = source[start:source.index("\n}\n", start)]
    # only an agent step that opts out of TDD (the eval `change`) says so; a gate like `setup` calls no agent
    assert 'step.type === "agent" && !step.tdd' in block
    assert 'gate: "no agent"' in block and 'ci: "runner action"' in block
    assert '?? "no skill (no TDD for prompt text)"' not in block


def test_a_phase_heading_keeps_its_name_apart_from_its_approval():
    source = page_source()
    rule = next(line for line in source.splitlines() if line.startswith(".col h2 {"))
    assert "gap:" in rule and "flex-wrap: wrap" in rule  # a long approval note wraps, never runs into "PLAN"


def test_the_new_task_form_asks_the_kind_of_work_with_radios():
    source = page_source()
    block = source[source.index("function newTaskDialog"):source.index("function renderBoard")]
    assert "What kind of work?" in block and 'type: "radio"' in block and "workLabel(name)" in block
    assert "nt-lane" not in block
    assert "body = { lane: kind()" in block  # the API still takes `lane`
    assert 'el("fieldset"' in block and 'el("legend"' in block
    assert "fields.replaceChildren(...parts.filter(Boolean))" in block  # `replaceChildren` prints a null as "null"


def test_every_lane_label_is_shown_in_the_real_config():
    from agentflow.config import load_config

    cfg = load_config(PYTHON_CONFIG)
    labels = cfg.raw["dashboard"]["backlog"]["lane_labels"]
    assert sorted(labels) == sorted(cfg.lanes)


def test_the_page_wires_the_backlog_actions():
    source = page_source()
    for needle in ('"/api/backlog"', '"/api/backlog/start"', '"/api/backlog/remove"', "+ New task", "Stop after",
                   "Can run in parallel", "Runs after:", "Parallel with:", "Start", "Remove", "after_choices",
                   "new_task", "lane_phases", "sync_note", "first_phase", "issues_note", "Taken elsewhere",
                   "issueNode"):
        assert needle in source, needle
    allow = re.search(r"\^\(type\|value[^\n]*", source).group(0)
    for attribute in ("disabled", "maxlength", "multiple"):
        assert attribute in allow, attribute


def test_a_blocked_backlog_card_gets_no_start_button_or_drag_handle():
    source = page_source()
    block = source[source.index("function backlogNode"):source.index("function renderBoard")]
    assert 'const canStart = item.status === "parallel" || item.status === "ready"' in block  # not blocked, not starting
    assert "draggable: canStart" in block and "if (canStart) buttons.push" in block and "if (canStart) dragFrom" in block


def test_a_message_raised_while_the_dialog_is_open_shows_inside_it():
    """`showModal()` puts the dialog in the top layer, above any z-index: a #toast there is hidden under it."""
    source = page_source()
    markup = source[source.index('<dialog id="dlg">'):source.index("</dialog>")]
    assert '<p id="dlg-msg" class="gate-bad" role="alert" hidden></p>' in markup
    assert markup.index('id="dlg-msg"') < markup.index('class="dlg-buttons"')  # under the fields, above OK
    css = source[:source.index("</style>")]
    assert re.search(r"#dlg-msg\s*\{[^}]*overflow-wrap:\s*anywhere", css)
    helpers = source[source.index("function clearDialogMessage()"):source.index("async function api(")]
    assert "function showDialogMessage(text)" in helpers and '$("dlg-msg")' in helpers
    assert "textContent = text" in helpers and "innerHTML" not in helpers
    toast = helpers[helpers.index("function toast(text)"):]
    # only the open dialog's own OK collects a message, unseen until it knows the outcome: a success is never red
    assert "if (dialogSaid !== null) { dialogSaid = text; return; }" in toast
    assert '$("dlg").open' not in toast  # an unrelated toast never lands in the dialog


def test_the_dialog_clears_its_message_and_floats_a_success_message_once_closed():
    source = page_source()
    block = source[source.index("function dialog(title"):source.index("function rejectDialog")]
    assert block.count("clearDialogMessage()") >= 2  # on open, and again before each OK
    assert block.index("clearDialogMessage()") < block.index("dlg.showModal()")
    ok = block[block.index('ok.addEventListener("click"'):block.index('cancel.addEventListener')]
    assert "clearDialogMessage()" in ok
    assert re.search(r'dialogSaid = "";[^}]*try \{ done = await onOk\(\); \} finally \{[^}]*dialogSaid = null;', ok)
    assert re.search(r"dlg\.close\(\);\s*if \(said\) toast\(said\)", ok)  # a success message floats after close
    assert "else if (said) showDialogMessage(said)" in ok  # a failure's message stays in the dialog


def test_a_submit_that_outlives_its_dialog_neither_closes_nor_writes_into_the_next():
    """OK, then Escape (or another dialog) before the answer: the late answer floats and closes nothing."""
    source = page_source()
    block = source[source.index("function dialog(title"):source.index("function rejectDialog")]
    assert "const run = ++dialogRun;" in block
    ok = block[block.index('ok.addEventListener("click"'):block.index('cancel.addEventListener')]
    late = "if (run !== dialogRun || !dlg.open) { if (said) toast(said); return; }"
    assert late in ok and ok.index(late) < ok.index("dlg.close()")


# --------------------------------------------------------------------------- time and cost


def finished_run(flow_repo, module: str = "crm") -> Run:
    run = flow_repo.new_run({"module": module})
    run.emit("phase_started", phase="plan")
    run.emit("agent_started", agent="claude", pid=11, model="opus", effort="high", step="plan/plan", role="plan")
    run.emit("agent_session", session_id="m-1", model="claude-opus-5-5", key="plan/plan", pid=11)
    run.emit("agent_exited", session_id="m-1", exit_code=0, cost=1.25, key="plan/plan")
    run.emit("gate_result", key="plan/plan", gate="floor", passed=True, skipped=False, duration_s=0.0)
    run.emit("run_finished")
    return run


def test_metrics_route_shows_this_months_runs_by_phase_and_model(dash):
    run = finished_run(dash.flow)
    status, _, body = dash.get("/api/metrics")
    data = payload(body)
    assert status == 200 and data["from"] == datetime.now().astimezone().date().replace(day=1).isoformat()
    (row,) = data["runs"]
    assert row["run_id"] == run.id and row["module"] == "crm" and row["metrics"]["cost_usd"] == 1.25
    (plan,) = row["metrics"]["phases"]
    assert plan["models"][0] | {"agent_s": 0} == {"agent": "claude", "model": "claude-opus-5-5", "effort": "high",
                                                  "sessions": 1, "agent_s": 0, "cost_usd": 1.25}
    assert data["totals"]["cost_usd"] == 1.25 and data["totals"]["models"][0]["model"] == "claude-opus-5-5·high"
    detail = payload(dash.get(f"/api/runs/{run.id}")[2])
    assert detail["metrics"]["phases"][0]["phase"] == "plan"


def test_metrics_route_filters_by_date_range_and_lane(dash):
    finished_run(dash.flow)
    assert payload(dash.get("/api/metrics?from=2000-01-01&to=2000-01-31")[2])["runs"] == []
    assert len(payload(dash.get("/api/metrics?all=1")[2])["runs"]) == 1
    today = datetime.now().astimezone().date().isoformat()
    assert len(payload(dash.get(f"/api/metrics?from={today}&to={today}")[2])["runs"]) == 1
    assert payload(dash.get("/api/metrics?lane=bug")[2])["runs"] == []
    assert dash.get("/api/metrics?lane=nope")[0] == 404
    assert dash.get("/api/metrics?from=yesterday")[0] == 400
    assert dash.get("/api/metrics?from=2026-11-05&to=2026-11-01")[0] == 400
    assert dash.get("/api/metrics", token=None)[0] == 403


def test_scorecard_api_needs_the_token_and_returns_the_view(dash):
    assert dash.get("/api/scorecard", token=None)[0] == 403
    assert dash.get("/api/scorecard", token="wrong")[0] == 403
    status, _, body = dash.get("/api/scorecard")
    data = payload(body)
    assert status == 200 and set(data) == {"baseline", "lanes"}
    assert data["baseline"] is False and data["lanes"] == {}
    assert dash.get("/api/scorecard?lane=nope")[0] == 404
    assert dash.get("/api/scorecard?from=yesterday")[0] == 400


def test_metrics_view_has_a_scorecard_section(dash):
    page = dash.get("/", token=None)[2].decode("utf-8")
    metrics = page[page.index('id="metrics-view"'):]
    assert 'id="scorecard"' in metrics


def test_the_scorecard_compares_with_runs_after_the_baseline_unless_the_range_is_custom(dash):
    page = page_source()
    scorecard = page[page.index('id="scorecard"'):]
    assert scorecard.index("Scorecard</h2>") < scorecard.index('<p id="sc-range" class="muted"></p>') < scorecard.index('id="sc-body"')
    query = page[page.index("function scorecardQuery()"):page.index("async function loadScorecard()")]
    assert 'metricsUi.range === "custom"' in query and "metricsQuery" not in query
    assert query.count('params.set("from"') == 1 and "custom && metricsUi.from" in query
    assert '"Runs after the baseline"' in query and '"Runs that ended "' in query
    assert '$("sc-range").textContent' in query and "innerHTML" not in query
    load = page[page.index("async function loadScorecard()"):]
    assert load[:400].count("scorecardQuery()") == 1 and "metricsQuery()" not in load[:400]


def test_old_runs_stay_in_metrics_and_on_the_board(flow_repo):
    from datetime import UTC, timedelta

    from agentflow.views import board, metrics_view

    run = finished_run(flow_repo)
    later = datetime.now(UTC) + timedelta(days=62)
    assert metrics_view(flow_repo.root, flow_repo.cfg, now=later)["runs"] == []  # not started this month
    assert [r["run_id"] for r in metrics_view(flow_repo.root, flow_repo.cfg, now=later, all_time=True)["runs"]] == [run.id]
    assert [c["run_id"] for c in board(flow_repo.root, flow_repo.cfg, now=later)["cards"]] == [run.id]
    card = board(flow_repo.root, flow_repo.cfg, now=later)["cards"][0]
    assert card["wall_s"] >= 0 and card["spend"] == 1.25


@pytest.fixture(autouse=True)
def _no_cached_board_figures():
    from agentflow import views

    views._DIGEST_CACHE.clear()
    yield
    views._DIGEST_CACHE.clear()


def _count_reads(monkeypatch, run):
    """The run's event-log reads by the board's own figures (other readers, such as `list_runs`, are not counted)."""
    from agentflow import views

    reads = []
    real = views._digest

    def digest(run_, today, events=None):
        reads.append(run_.events.path)
        return real(run_, today, events)

    monkeypatch.setattr(views, "_digest", digest)
    return reads


def test_the_board_computes_an_unchanged_runs_figures_once(dash, monkeypatch):
    from agentflow.views import board

    run = finished_run(dash.flow)
    reads = _count_reads(monkeypatch, run)
    board(dash.flow.root, dash.flow.cfg)
    board(dash.flow.root, dash.flow.cfg)
    assert reads.count(run.events.path) == 1
    run.emit("run_archived", by="me")
    board(dash.flow.root, dash.flow.cfg)
    assert reads.count(run.events.path) == 2  # a changed log is read again


def test_a_live_runs_cached_figures_expire(dash, monkeypatch):
    from datetime import UTC, timedelta

    from agentflow.views import board

    run = awaiting(dash.flow)
    run.emit("step_started", key="plan/plan")  # not ended: its metrics are live
    reads = _count_reads(monkeypatch, run)
    t0 = datetime.now(UTC)
    first = board(dash.flow.root, dash.flow.cfg, now=t0)
    assert first["cards"][0]["run_id"] == run.id
    board(dash.flow.root, dash.flow.cfg, now=t0 + timedelta(seconds=5))
    assert len(reads) == 1
    later = board(dash.flow.root, dash.flow.cfg, now=t0 + timedelta(seconds=31))
    assert len(reads) == 2
    assert later["cards"][0]["wall_s"] > first["cards"][0]["wall_s"]  # a quiet live run's wall time keeps counting


def test_an_ended_runs_cached_figures_never_expire_but_a_new_day_misses(dash, monkeypatch):
    from datetime import UTC, timedelta

    from agentflow.views import board

    run = finished_run(dash.flow)
    reads = _count_reads(monkeypatch, run)
    t0 = datetime.now(UTC)
    board(dash.flow.root, dash.flow.cfg, now=t0)
    board(dash.flow.root, dash.flow.cfg, now=t0 + timedelta(seconds=3600))
    assert len(reads) == 1 or t0.astimezone().date() != (t0 + timedelta(seconds=3600)).astimezone().date()
    reads.clear()
    board(dash.flow.root, dash.flow.cfg, now=t0 + timedelta(days=2))  # spend_today depends on the date
    assert len(reads) == 1


def test_board_figures_cache_is_capped_and_a_failed_stat_skips_it(dash, monkeypatch):
    from datetime import UTC

    from agentflow import views

    run = finished_run(dash.flow)
    now = datetime.now(UTC)
    monkeypatch.setattr(views, "_METRICS_CACHE_MAX", 2)
    views._DIGEST_CACHE.update({("a",): None, ("b",): None})
    views._board_figures(run, dash.flow.cfg, now)
    assert len(views._DIGEST_CACHE) == 2 and ("a",) not in views._DIGEST_CACHE  # the oldest is evicted first
    views._DIGEST_CACHE.clear()
    reads = _count_reads(monkeypatch, run)
    real_stat = Path.stat

    def failing(self, *a, **k):
        if self == run.events.path and sys._getframe(1).f_code.co_name == "_board_figures":
            raise OSError("gone")  # only the board's own stat fails; reading the log still works
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", failing)
    digest, metrics = views._board_figures(run, dash.flow.cfg, now)
    views._board_figures(run, dash.flow.cfg, now)
    assert not views._DIGEST_CACHE and len(reads) == 2 and metrics["cost_usd"] == 1.25 and digest["fixes"] == {}


def test_the_board_carries_the_done_modules_and_the_note_and_a_lane_that_is_not_the_module_lane_sees_none(bdash):
    from agentflow import finished

    finished.mark(bdash.flow.root, "c", "test")
    data = payload(bdash.get("/api/board")[2])
    assert data["backlog_done"] == [{"key": "module:c", "id": "c", "reason": "done by hand"}]
    assert data["done_note"] == "" and "module:c" not in {c["key"] for c in data["backlog"]}
    other = payload(bdash.get("/api/board?lane=bug")[2])
    assert other["backlog_done"] == [] and "done_note" in other
    assert payload(bdash.get("/api/board?lane=feature")[2])["backlog_done"] == data["backlog_done"]


def test_the_page_shows_the_done_modules_in_a_details_that_keeps_its_open_state():
    source = page_source()
    block = source[source.index("function doneNode"):source.index("function renderBoard")]
    assert 'el("details"' in block and '"Done (" +' in block and "ontoggle" in block
    assert "ui.doneOpen" in block and "open: ui.doneOpen" in block
    assert "item.reason" in block and "Start" not in block  # one line each, no Start button
    assert "innerHTML" not in block  # every text goes through el()'s textContent
    assert "data.done_note" in source and "doneNode(data.backlog_done" in source
    assert "ontoggle" in source[source.index("function el("):source.index("function toast")]


# --------------------------------------------------------------------------- the Lessons tab


def test_lessons_api_needs_the_token(dash):
    assert dash.get("/api/lessons", token=None)[0] == 403


def test_lessons_api_lists_the_lessons_and_their_effects(dash):
    (dash.flow.install_pack().parent / "lessons.md").write_text(
        "## L-0001 Run lint\n- source: 20261101-0800-feature-r0#3\n- target: gate_first_pass:lint\nRun it.\n",
        encoding="utf-8")
    status, headers, body = dash.get("/api/lessons")
    data = payload(body)
    assert status == 200 and headers["content-type"].startswith("application/json")
    rows = {r["id"]: r for r in data["lessons"]}
    assert rows["E-0001"]["verdict"] == "not measured" and rows["L-0001"]["verdict"] == "too few runs"
    assert rows["L-0001"]["sources"][0]["run_id"] == "20261101-0800-feature-r0"
    assert data["note"] == "Before/after compares runs; it shows correlation, not cause."


def test_the_lessons_tab_links_evidence_to_the_run_page_and_escapes_text():
    source = page_source()
    assert 'id="tab-lessons"' in source and 'id="lessons-view"' in source and '"lessons"' in source
    block = source[source.index("function sourceNodes"):source.index("async function loadLessons")]
    assert "goRun(src.run_id)" in block and "data.note" in block
    assert "innerHTML" not in block  # titles and bodies are model text: every text goes through el()'s textContent
    assert 'api("/api/lessons")' in source


def test_board_carries_retro_due(dash):
    from datetime import UTC, timedelta

    from test_retro import _finished

    _, _, body = dash.get("/api/board")
    assert payload(body)["retro_due"] is None
    _finished(dash.flow.root, start=datetime.now(UTC) - timedelta(days=10))
    _, _, body = dash.get("/api/board")
    assert payload(body)["retro_due"] == "1 finished runs since the start"


def test_the_page_shows_the_retro_banner_from_safe_text_only():
    source = page_source()
    assert 'id="retro-due"' in source and "data.retro_due" in source and '"agentflow retro"' in source
    assert "innerHTML" not in source


def test_the_spec_routes_read_through_the_artifact_list(dash, spec_run):
    rid = spec_run.run.id
    listed = payload(dash.get(f"/api/runs/{rid}/artifacts")[2])
    spec = next(a for a in listed if a["path"] == "docs/specs/x.md")
    r1, r2 = spec["revisions"][0]["sha"], spec["revisions"][1]["sha"]
    got = payload(dash.get(f"/api/runs/{rid}/artifact?path=docs/specs/x.md&sha={r2}")[2])
    assert got["sha"] == r2 and got["lines"] > 0
    diff = payload(dash.get(f"/api/runs/{rid}/artifact/diff?path=docs/specs/x.md&from={r1}&to={r2}")[2])
    assert "+Scope 3 says another" in diff["diff"]
    assert payload(dash.get(f"/api/runs/{rid}/findings")[2]) == []


def test_head_reads_the_branch_tip_after_a_new_commit(dash, spec_run):
    rid = spec_run.run.id
    path = f"/api/runs/{rid}/artifact?path=docs/specs/x.md&sha=head"
    before = payload(dash.get(path)[2])["sha"]
    new = spec_run.commit("define/spec_reconcile", write={"docs/specs/x.md": "# X\n\nlater\n"})
    after = payload(dash.get(path)[2])
    assert before != new and after["sha"] == new


@pytest.mark.parametrize("query", ["path=..%2F..%2Fstate.json&sha=head", "path=README.md&sha=head",
                                   "path=docs%2Fspecs%2Fx.md&sha=" + "0" * 40])
def test_the_artifact_route_refuses_what_is_not_listed(dash, spec_run, query):
    assert dash.get(f"/api/runs/{spec_run.run.id}/artifact?{query}")[0] == 404


def assert_refusal_matrix(dash, run, route):
    rid = run.id
    assert dash.get(f"/api/runs/{rid}/{route}", token=None)[0] == 403
    assert dash.get(f"/api/runs/{rid}/{route}", headers={"Host": "evil.example"})[0] == 403
    assert dash.get(f"/api/runs/{rid}/{route}", headers={"Origin": "http://evil.example"})[0] == 403
    assert dash.get(f"/api/runs/20261030-0000-feature-nope/{route}")[0] == 404
    assert dash.post(f"/api/runs/{rid}/{route}")[0] == 404
    link = runs_root(dash.flow.root) / "20261030-0000-feature-link"
    link.symlink_to(run.dir, target_is_directory=True)
    assert dash.get(f"/api/runs/{link.name}/{route}")[0] == 404


@pytest.mark.parametrize("route", ["artifacts", "artifact?path=docs%2Fspecs%2Fx.md&sha=head",
                                   "artifact/diff?path=docs%2Fspecs%2Fx.md&from=head&to=head", "findings"])
def test_the_artifact_routes_keep_the_refusal_matrix(dash, spec_run, route):
    assert_refusal_matrix(dash, spec_run.run, route)


def test_the_process_routes_answer(dash, spec_run):
    rid = spec_run.run.id
    assert payload(dash.get(f"/api/runs/{rid}/plan")[2])["path"] in ("docs/plans/x.md", None)
    (spec_run.run.dir / "issue.md").write_text("# Ask\n", encoding="utf-8")
    assert {"name": "issue.md", "group": "request", "size": 6} in payload(dash.get(f"/api/runs/{rid}/files")[2])
    assert payload(dash.get(f"/api/runs/{rid}/files?name=issue.md")[2])["text"] == "# Ask\n"
    changes = payload(dash.get(f"/api/runs/{rid}/changes")[2])
    assert "docs/specs/x.md" in {f["path"] for f in changes["files"]}
    diff = payload(dash.get(f"/api/runs/{rid}/changes?path=docs%2Fspecs%2Fx.md")[2])
    assert "+Scope 3 says another" in diff["diff"]


@pytest.mark.parametrize("route", ["plan", "files", "files?name=issue.md", "changes", "changes?path=notes.md"])
def test_the_process_routes_keep_the_refusal_matrix(dash, spec_run, route):
    assert_refusal_matrix(dash, spec_run.run, route)


@pytest.mark.parametrize("query", ["files?name=..%2Fevents.jsonl", "files?name=state.json",
                                   "changes?path=..%2F..%2Fetc%2Fpasswd", "changes?path=README.md"])
def test_the_process_routes_refuse_what_is_not_listed(dash, spec_run, query):
    assert dash.get(f"/api/runs/{spec_run.run.id}/{query}")[0] == 404


def test_a_doubled_name_is_a_400(dash, spec_run):
    rid = spec_run.run.id
    assert dash.get(f"/api/runs/{rid}/files?name=pr.md&name=issue.md")[0] == 400
    assert dash.get(f"/api/runs/{rid}/changes?path=a&path=b")[0] == 400


def test_a_missing_or_doubled_parameter_is_a_400(dash, spec_run):
    rid = spec_run.run.id
    status, _, body = dash.get(f"/api/runs/{rid}/artifact?path=docs%2Fspecs%2Fx.md")
    assert status == 400 and payload(body)["error"] == "send exactly one sha"
    assert dash.get(f"/api/runs/{rid}/artifact?path=a&path=b&sha=head")[0] == 400
    assert dash.get(f"/api/runs/{rid}/artifact/diff?path=a&from=head")[0] == 400


def test_a_big_artifact_is_cut_to_the_configured_cap(dash, spec_run):
    dash.server.cfg.dashboard["artifact_max_kb"] = 16
    big = spec_run.commit("define/spec_reconcile", write={"docs/specs/x.md": ("x" * 39 + "\n") * 3000})
    got = payload(dash.get(f"/api/runs/{spec_run.run.id}/artifact?path=docs%2Fspecs%2Fx.md&sha={big}")[2])
    assert got["truncated"] is True and len(got["text"].encode()) <= 16 * 1024


def test_run_detail_names_the_artifact_tabs_its_lane_walks(dash):
    run = awaiting(dash.flow)
    assert payload(dash.get(f"/api/runs/{run.id}")[2])["artifact_tabs"] == ["spec", "plan"]


def test_the_run_page_has_spec_and_plan_tabs_only_where_the_lane_walks_them():
    run_js = (UI_DIR / "run.js").read_text(encoding="utf-8")
    assert 'RUN_TABS = ["overview", "spec", "plan", "activity", "gates", "files"]' in run_js
    assert "visibleTabs(detail)" in run_js and "artifact_tabs" in (UI_DIR / "artifact.js").read_text(encoding="utf-8")


def test_the_spec_tab_reads_the_artifact_routes_and_says_when_there_is_none():
    art = (UI_DIR / "artifact.js").read_text(encoding="utf-8")
    for needle in ("/artifacts", "/artifact?path=", "/artifact/diff?path=", "/findings", "has not committed one",
                   "Showing the first", "Copy path", "as merged", "Diff with previous"):
        assert needle in art, needle
    assert UI_SCRIPTS.index("run.js") < UI_SCRIPTS.index("artifact.js") < UI_SCRIPTS.index("main.js")


def test_a_failed_findings_read_is_shown_not_swallowed():
    art = (UI_DIR / "artifact.js").read_text(encoding="utf-8")
    assert ".catch(() => null)" not in art
    assert "Could not load the findings: " in art and "Could not refresh the findings: " in art
    assert "Could not read " in art and "art:doc-error" in art  # a failed first read says why in the tab


def test_the_note_cap_matches_the_server():
    from agentflow.dashboard import TEXT_MAX
    assert f"NOTE_MAX = {TEXT_MAX}" in (UI_DIR / "artifact.js").read_text(encoding="utf-8")


def test_the_viewed_cap_matches_the_server():
    from agentflow.dashboard import VIEWED_MAX
    assert f"VIEWED_CAP = {VIEWED_MAX}" in (UI_DIR / "artifact.js").read_text(encoding="utf-8")


def test_returning_to_an_artifact_tab_refetches_its_list_and_findings():
    run_js = (UI_DIR / "run.js").read_text(encoding="utf-8")
    opener = run_js[run_js.index("function openRun"):run_js.index("function showRun")]
    assert "reopensArtifact(prev, id, tab, ui.artifact)" in opener and "loadArtifacts(id)" in opener


def test_a_phase_approval_from_the_page_sends_what_was_viewed():
    board_js = (UI_DIR / "board.js").read_text(encoding="utf-8")
    flow = board_js[board_js.index("function approveFlow"):board_js.index("function needNode")]
    assert "viewedList(item.run_id)" in flow and "viewed" in flow
    art = (UI_DIR / "artifact.js").read_text(encoding="utf-8")
    assert "ui.viewed" in art and "ui.comments" in art and "no longer waiting on you" in art
    assert "function rejectDialog(runId, target, initial" in (UI_DIR / "dialogs.js").read_text(encoding="utf-8")


def test_the_plan_tab_has_a_tasks_mode():
    plan_js = (UI_DIR / "plan.js").read_text(encoding="utf-8")
    for needle in ("/plan", "blocks: ", "independent: ", "No plan for this run yet", "Open decision", "ui.plan"):
        assert needle in plan_js, needle
    assert UI_SCRIPTS.index("artifact.js") < UI_SCRIPTS.index("plan.js") < UI_SCRIPTS.index("main.js")


def test_the_files_tab_lists_the_run_documents_and_the_changes():
    files_js = (UI_DIR / "files.js").read_text(encoding="utf-8")
    for needle in ("/files", "/files?name=", "/changes", "/changes?path=", "Changes since the base",
                   "No branch yet", "Showing the first", "Transcript tail", "Step records", "ui.files"):
        assert needle in files_js, needle
    run_js = (UI_DIR / "run.js").read_text(encoding="utf-8")
    files_branch = run_js[run_js.index('tab === "files"'):run_js.index("}", run_js.index('tab === "files"'))]
    assert "fileEntries(" in files_branch and '"file:handoff"' not in run_js and '"file:progress"' not in run_js
    assert UI_SCRIPTS.index("plan.js") < UI_SCRIPTS.index("files.js") < UI_SCRIPTS.index("main.js")


def test_no_two_ui_scripts_declare_the_same_top_level_name():
    # every script shares one global scope: a later declaration silently replaces an earlier one
    decl = re.compile(r"^(?:async\s+function\s*\*?|function\s*\*?|const|let|class)\s+([A-Za-z_$][\w$]*)", re.M)
    seen: dict[str, str] = {}
    clashes = []
    for name in UI_SCRIPTS:
        for found in decl.findall((UI_DIR / name).read_text(encoding="utf-8")):
            if found in seen and seen[found] != name:
                clashes.append(f"{found}: {seen[found]} and {name}")
            seen.setdefault(found, name)
    assert not clashes, clashes
