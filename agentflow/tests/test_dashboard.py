"""The local dashboard (Task 13): token and origin checks, the JSON API, actions, SSE and the page."""

from __future__ import annotations

import http.client
import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest

from agentflow import hitl
from agentflow.dashboard import make_server
from agentflow.state import Run
from agentflow.views import _badge
from conftest import run_git

TOKEN = "k3y-" + "x" * 40
AGENTFLOW = Path(__file__).resolve().parents[1]


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
    assert payload(body)["state"]["refine"]["note"] == note


def test_page_is_served_with_a_nonce_and_no_unsafe_dom_sinks(dash):
    status, headers, body = dash.get("/", token=None)
    assert status == 200 and headers["content-type"].startswith("text/html")
    csp = headers["content-security-policy"]
    nonce = re.search(r"'nonce-([A-Za-z0-9_\-]+)'", csp).group(1)
    page = body.decode("utf-8")
    assert f'<script nonce="{nonce}">' in page and "{{" not in page
    assert "connect-src 'self'" in csp and "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp
    assert dash.get("/", token=None)[1]["content-security-policy"] != csp  # a fresh nonce each time

    source = (AGENTFLOW / "dashboard.html").read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                 "setTimeout(\"", "setTimeout('", " style=\"", "javascript:"):
        assert sink not in source, sink
    assert not re.search(r"https?://", source)  # no external scripts, fonts or requests
    assert "textContent" in source and "EventSource" in source and "prefers-color-scheme" in source
    assert "X-Agentflow-Token" in source


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
    assert "agentflow dashboard" in page  # it tells a visitor without a token where the URL comes from
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
    source = (AGENTFLOW / "dashboard.html").read_text(encoding="utf-8")
    script = source.split("<script", 1)[1]
    boot = script[script.index("const TOKEN"):script.index("const ui")]
    assert "location.hash" in boot  # the first visit reads #token=...
    assert 'sessionStorage.getItem("agentflow-token")' in boot  # a reload falls back to the stored token
    assert 'sessionStorage.setItem("agentflow-token"' in boot
    assert "history.replaceState" in boot and "location.search" not in boot  # the fragment is removed; no query token
    assert "open the URL printed by `agentflow dashboard`" in source  # what a tokenless visit shows


def test_run_drawer_never_hands_null_to_replace_children():
    """The DOM prints a null argument as the text "null"; only `el()` skips null children."""
    source = (AGENTFLOW / "dashboard.html").read_text(encoding="utf-8")
    block = source[source.index("drawer.replaceChildren(\n"):source.index("function closeDrawer")]
    assert not re.search(r":\s*null\b", block)


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
    assert (bdash.flow.root / "devtools/agentflow/runs/queue.json").is_file()
    assert bdash.fake.calls == []


def test_a_failing_refresh_round_does_not_stop_the_loop(bdash, monkeypatch, capsys):
    from agentflow import backlog
    from agentflow.dashboard import refresh_once

    monkeypatch.setattr(backlog, "refresh", lambda cfg, repo: (_ for _ in ()).throw(OSError("disk full")))
    refresh_once(bdash.server)
    assert "disk full" in capsys.readouterr().err


def test_the_page_names_no_lane_phase_or_path(bdash):
    """The page is framework code: lanes, phases and the form fields all come from the board data."""
    source = (AGENTFLOW / "dashboard.html").read_text(encoding="utf-8")
    cfg = bdash.flow.cfg
    for word in [*cfg.lanes, "CAPABILITY-MAP", "docs/", "origin/", "flow/"]:
        assert not re.search(rf"(?<![A-Za-z-]){re.escape(word)}(?![A-Za-z-])", source), word
    # a phase word may appear in prose, but never as a string the script compares against
    for phase in cfg.phases:
        assert f'"{phase}"' not in source and f"'{phase}'" not in source, phase


def test_the_page_shows_one_workflow_with_no_lane_filter():
    source = (AGENTFLOW / "dashboard.html").read_text(encoding="utf-8")
    for gone in ('id="lane"', "ui.lane", 'lane: ""', "All lanes", "inLane", '"?lane="', '"Lane · phase › step"'):
        assert gone not in source, gone
    for card in ("text: card.lane", "text: item.lane"):
        assert card not in source, card  # a card names its work, not its lane
    assert "Phase › step" in source and "text: r.place" in source


def test_a_workflow_step_is_badged_only_when_it_runs_for_one_kind_of_work():
    source = (AGENTFLOW / "dashboard.html").read_text(encoding="utf-8")
    block = source[source.index("function onlyFor"):source.index("function renderWorkflow")]
    assert "step.lanes.length !== 1" in block  # a step for two lanes or more carries no badge
    assert 'workLabel(step.lanes[0]) + " only"' in block
    assert "parent" in block  # a nested step doesn't repeat the badge of the step around it


def test_a_step_with_no_skill_is_labelled_by_what_it_is():
    source = (AGENTFLOW / "dashboard.html").read_text(encoding="utf-8")
    start = source.index("function stepSkills")
    block = source[start:source.index("\n}\n", start)]
    # only an agent step that opts out of TDD (the eval `change`) says so; a gate like `setup` calls no agent
    assert 'step.type === "agent" && !step.tdd' in block
    assert 'gate: "no agent"' in block and 'ci: "runner action"' in block
    assert '?? "no skill (no TDD for prompt text)"' not in block


def test_a_phase_heading_keeps_its_name_apart_from_its_approval():
    source = (AGENTFLOW / "dashboard.html").read_text(encoding="utf-8")
    rule = next(line for line in source.splitlines() if line.startswith(".col h2 {"))
    assert "gap:" in rule and "flex-wrap: wrap" in rule  # a long approval note wraps, never runs into "PLAN"


def test_the_new_task_form_asks_the_kind_of_work_with_radios():
    source = (AGENTFLOW / "dashboard.html").read_text(encoding="utf-8")
    block = source[source.index("function newTaskDialog"):source.index("function renderBoard")]
    assert "What kind of work?" in block and 'type: "radio"' in block and "workLabel(name)" in block
    assert "nt-lane" not in block
    assert "body = { lane: kind()" in block  # the API still takes `lane`
    assert 'el("fieldset"' in block and 'el("legend"' in block
    assert "fields.replaceChildren(...parts.filter(Boolean))" in block  # `replaceChildren` prints a null as "null"


def test_every_lane_label_is_shown_in_the_real_config():
    from agentflow.config import load_config

    cfg = load_config(AGENTFLOW / "config.yaml")
    labels = cfg.raw["dashboard"]["backlog"]["lane_labels"]
    assert sorted(labels) == sorted(cfg.lanes)


def test_the_page_wires_the_backlog_actions():
    source = (AGENTFLOW / "dashboard.html").read_text(encoding="utf-8")
    for needle in ('"/api/backlog"', '"/api/backlog/start"', '"/api/backlog/remove"', "+ New task", "Stop after",
                   "Can run in parallel", "Runs after:", "Parallel with:", "Start", "Remove", "after_choices",
                   "new_task", "lane_phases", "sync_note", "first_phase"):
        assert needle in source, needle
    allow = re.search(r"\^\(type\|value[^\n]*", source).group(0)
    for attribute in ("disabled", "maxlength", "multiple"):
        assert attribute in allow, attribute


def test_a_blocked_backlog_card_gets_no_start_button_or_drag_handle():
    source = (AGENTFLOW / "dashboard.html").read_text(encoding="utf-8")
    block = source[source.index("function backlogNode"):source.index("function renderBoard")]
    assert 'const canStart = item.status === "parallel" || item.status === "ready"' in block  # not blocked, not starting
    assert "draggable: canStart" in block and "if (canStart) buttons.push" in block and "if (canStart) dragFrom" in block


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
