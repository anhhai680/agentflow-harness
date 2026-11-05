"""The local dashboard: a stdlib HTTP server for `dashboard.html`, a JSON API over the runs, Server-Sent
Events for live updates, and the human actions.

Routes:
- `GET /` (exactly `/`) is the page shell: it holds no run data, so it needs no token. `agentflow dashboard`
  prints `http://127.0.0.1:<port>/#token=<token>`; the fragment never reaches the server or its log, and
  the page keeps the token in `sessionStorage`, so a reload works. Every other route needs the token.
- `GET /api/board` (every lane, or `?lane=<name>`), `GET /api/workflow`, `GET /api/agents`,
  `GET /api/runs/<id>` (the card detail, its time and cost included), `GET /api/metrics` (time and cost over
  the runs started in `?from=YYYY-MM-DD&to=YYYY-MM-DD`, this month by default, or `?all=1`; `&lane=<name>`
  narrows it; a bad date is a 400); `GET /api/events` (SSE: every run's new events, runs created
  later included).
- `POST /api/runs/<id>/{approve,reject,answer,pause,resume,done,cancel,retry}` with a JSON body. Each action
  calls the same function as the CLI with `by="dashboard"`; reject, a complete answer, resume, done, retry and
  an approval inside a phase then start `flow.py resume <id>` detached (D13), unless a runner already holds
  the run's lock. A phase approval starts nothing: the phase rests as ready until a human continues it
  (`resume`) or marks it done (`done`, which ends in the draft pull request) (D16).
  An action the run is not in a state for (a second approve, a live runner) answers 409 with the reason.
- `POST /api/backlog` (add a card: `lane`, `fields`, `after`, `to_phase`), `POST /api/backlog/start` (`module` or
  `item`) and `POST /api/backlog/remove` (`item`). A start re-checks that the card is not blocked and has no
  duplicate (409 with the reason otherwise), answers 202 at once and runs the slow part (`flow.py run --detach`)
  on a worker thread, one start at a time. Nothing starts by itself: only these calls start work.
- `GET /api/config` (the Settings page: the config's text, hash and form fields), `POST /api/config/preview` and
  `POST /api/config/save` with `base_sha256` and either `changes` (the form) or `text` (the YAML tab); see
  `config_edit`. A save is refused unless the new config validates, leaves the protected settings as they are and
  starts from the file's current hash (409 otherwise). After a save the dashboard serves the new config at once,
  and a live runner on the pack config picks it up when its next phase starts.

It can approve checkpoints and start agents, so it is locked down:
- it binds `127.0.0.1` only, and every request but the data-free shell needs the random token printed at
  start (the `X-Agentflow-Token` header, or `?token=`, which the event stream uses because `EventSource`
  cannot set headers; compared in constant time);
  anything else gets an empty 403;
- the `Host` header must be `127.0.0.1:<port>` or `localhost:<port>` (DNS rebinding), and an `Origin`,
  when sent, must be the dashboard's own;
- POST bodies are `application/json` objects of at most 64 KB (the config routes: the config's size limit plus
  64 KB); run ids must have the run-id shape and name
  an existing run directory, and files are read only through the run's own paths, size-capped;
- every response is `no-store`, `nosniff`, `no-referrer`, not frameable, with a strict CSP (the page's one
  script and style carry a fresh nonce); JSON escapes `<`, `>` and `&`, and the page renders all run text
  with `textContent`.
"""

from __future__ import annotations

import hmac
import json
import math
import re
import secrets
import sys
import threading
import time
import traceback
from collections.abc import Callable
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from agentflow import backlog, config_edit, hitl, module_queue
from agentflow.config import Config, ConfigError, for_lane, load_config
from agentflow.events import EventError, lock_state
from agentflow.run_control import ControlError, cancel, mark_done, pause, resume, retry, spawn_resume
from agentflow.state import Run, runs_root
from agentflow.views import agents_view, board, metrics_view, run_detail, workflow_view

PAGE = Path(__file__).resolve().with_name("dashboard.html")
MAX_BODY = 64 * 1024
MIN_TOKEN_CHARS = 22  # 128 bits of token_urlsafe
RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{4}-[a-z0-9]+(?:-[a-z0-9]+)*$")
RUN_ID_MAX = 120
RUN_ROUTE = re.compile(r"^/api/runs/([^/]+)$")
ACTION_ROUTE = re.compile(r"^/api/runs/([^/]+)/([a-z]+)$")
TEXT_MAX = backlog.TEXT_MAX
BACKLOG_ROUTES = ("/api/backlog", "/api/backlog/start", "/api/backlog/remove")
CONFIG_ROUTES = ("/api/config/preview", "/api/config/save")
CONFIG_MAX_BODY = config_edit.MAX_TEXT + 64 * 1024  # the YAML tab sends the whole file
BACKLOG_LOG = "backlog"  # the `run_id` the live stream gives backlog events
REFRESH_EVERY_S = 60.0
SSE_POLL_S = 0.25
SSE_PING_S = 15.0
REMIND_EVERY_S = 60.0
BY = "dashboard"
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
PAGE_CSP = ("default-src 'none'; script-src 'nonce-{n}'; style-src 'nonce-{n}'; connect-src 'self'; img-src data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

__all__ = ["DashboardServer", "dashboard_url", "make_server", "prepare", "refresh_once", "serve"]


def dashboard_url(port: int, token: str) -> str:
    """The URL `agentflow dashboard` prints: the token rides in the fragment, which browsers never send."""
    return f"http://127.0.0.1:{port}/#token={token}"


class BadRequest(Exception):
    """A request body that does not have the shape the action needs."""


def _json_bytes(data: Any) -> bytes:
    text = json.dumps(data, ensure_ascii=False, default=str)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").encode("utf-8")


def _text(body: dict, name: str, required: bool = False) -> str | None:
    value = body.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value) > TEXT_MAX:
        raise BadRequest(f"{name} must be text of at most {TEXT_MAX} characters")
    return value


# --------------------------------------------------------------------------- actions


def _default_target(run: Run) -> str:
    waiting = run.state["waiting"]
    if waiting is None:
        raise hitl.NotWaiting(f"{run.id} is not awaiting approval of anything")
    return waiting["phase"] if waiting["kind"] == "phase_approval" else waiting["key"]


def _rests(run: Run, cfg: Config) -> bool:
    """Whether an approval rests the card on the board: only when a human decides on the next phase with work
    for this lane anyway, or none is left. Otherwise the run simply goes on, because nothing there waits for you."""
    waiting = run.state["waiting"] or {}
    if waiting.get("kind") != "phase_approval":
        return True  # `hold` means nothing for an inner stop
    names = list(cfg.phases)
    lane = run.state.get("pipeline")
    following = [cfg.phases[name] for name in names[names.index(waiting["phase"]) + 1 :]
                 if for_lane(cfg.phases[name].steps, lane)]
    return not following or following[0].needs_approval


def _approve(run: Run, body: dict, cfg: Config) -> tuple[dict, bool]:
    budget = body.get("budget")
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (int, float))
                               or not math.isfinite(budget)):
        raise BadRequest("budget must be a finite number")
    rested = hitl.approve(run, _text(body, "target") or _default_target(run), BY, budget=budget, hold=_rests(run, cfg),
                          note=_text(body, "note"))
    return {}, not rested


def _reject(run: Run, body: dict) -> tuple[dict, bool]:
    note = _text(body, "note", required=True) or ""
    hitl.reject(run, _text(body, "target") or _default_target(run), BY, note)
    return {}, True


def _answer(run: Run, body: dict) -> tuple[dict, bool]:
    answers = body.get("answers")
    if not isinstance(answers, dict) or not all(isinstance(k, str) and isinstance(v, str) and len(v) <= TEXT_MAX
                                                for k, v in answers.items()):
        raise BadRequest("answers must be an object of question id to answer text")
    record = body.get("record", False)
    if not isinstance(record, bool):
        raise BadRequest("record must be true or false")
    key = _text(body, "key")
    if key is None:
        item = hitl.pending_item(run)
        if item is None:
            raise hitl.NotWaiting(f"{run.id} is not waiting for answers")
        key = item["key"]
    complete = hitl.answer(run, key, answers, BY, record=record)
    pending = [] if complete else [q for q, text in hitl.parse_answers(run.inbox_path(key)).items() if not text]
    return {"complete": complete, "pending": pending}, complete


def _pause(run: Run, body: dict) -> tuple[dict, bool]:
    pause(run, BY)
    return {}, False


def _resume(run: Run, body: dict) -> tuple[dict, bool]:
    resume(run, BY)
    return {}, True


def _done(run: Run, body: dict) -> tuple[dict, bool]:
    mark_done(run, BY)
    return {}, True


def _cancel(run: Run, body: dict) -> tuple[dict, bool]:
    return {"killed": cancel(run, BY)}, False


def _retry(run: Run, body: dict) -> tuple[dict, bool]:
    return {"key": retry(run, _text(body, "key"), BY)}, True


ACTIONS: dict[str, Callable[[Run, dict], tuple[dict, bool]]] = {
    "reject": _reject, "answer": _answer, "pause": _pause,
    "resume": _resume, "done": _done, "cancel": _cancel, "retry": _retry,
}
CONFLICTS = (hitl.NotWaiting, ControlError, EventError)


# --------------------------------------------------------------------------- server


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], repo: Path, cfg: Config, token: str,
                 spawn: Callable[[Run], object], spawn_run: Callable[..., str] | None = None,
                 config: Path | None = None) -> None:
        super().__init__(address, _Handler)
        self.repo = repo
        self.cfg = cfg
        self.token = token
        self.spawn = spawn
        self.spawn_run = spawn_run or module_queue.spawn_run
        self.config = config  # the config.yaml this dashboard was started with, passed on to the runs it starts
        self.starts: list[threading.Thread] = []
        self.stopping = threading.Event()
        port = self.server_address[1]
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{host}" for host in self.hosts}

    def close(self) -> None:
        """Stop serving (from another thread than `serve_forever`) and end open event streams."""
        self.stopping.set()
        self.shutdown()
        self.server_close()


def make_server(repo: Path, cfg: Config, host: str, port: int, token: str,
                spawn: Callable[[Run], object] | None = None, spawn_run: Callable[..., str] | None = None,
                config: Path | None = None) -> DashboardServer:
    """A dashboard server bound to `host:port` (port 0 picks a free one); not yet serving."""
    if host != "127.0.0.1":
        raise ValueError(f"the dashboard binds 127.0.0.1 only, not {host!r}")
    if len(token) < MIN_TOKEN_CHARS:
        raise ValueError("the dashboard token must carry at least 128 bits")
    return DashboardServer((host, port), Path(repo).resolve(), cfg, token, spawn or spawn_resume, spawn_run, config)


def _remind_loop(server: DashboardServer) -> None:
    while not server.stopping.is_set():
        try:
            hitl.remind_due(server.cfg, server.repo)
        except (OSError, ValueError, EventError) as exc:
            sys.stderr.write(f"dashboard: reminders failed: {exc}\n")
        server.stopping.wait(REMIND_EVERY_S)


def prepare(server: DashboardServer) -> list[str]:
    """What runs once when the dashboard comes up: a start that was interrupted (the last dashboard died between
    recording it and its outcome) becomes a failure for a person to check. It is never repeated."""
    settled = backlog.settle_interrupted(server.repo)
    for item_id in settled:
        sys.stderr.write(f"dashboard: card {item_id} was starting when the last dashboard stopped; "
                         "it is marked failed (check before pressing Start again)\n")
    return settled


def refresh_once(server: DashboardServer) -> None:
    """One round of keeping Backlog current: base branch, map assessment, merge state. It starts nothing."""
    try:
        backlog.refresh(server.cfg, server.repo)
    except Exception as exc:  # a failed round must not end the loop; the next one tries again
        sys.stderr.write(f"dashboard: backlog refresh failed: {exc}\n")


def _refresh_loop(server: DashboardServer) -> None:
    while not server.stopping.is_set():
        refresh_once(server)
        server.stopping.wait(REFRESH_EVERY_S)


def serve(repo: Path, cfg: Config, host: str, port: int, token: str,
          announce: Callable[[str], object] = print, config: Path | None = None) -> None:
    """Serve the dashboard until Ctrl-C, sending due reminders and refreshing Backlog every minute."""
    server = make_server(repo, cfg, host, port, token, config=config)
    prepare(server)
    announce(f"agentflow dashboard: {dashboard_url(server.server_address[1], token)}\n"
             "Keep this URL private: the token lets anyone with it approve checkpoints. Ctrl-C stops the server.")
    threading.Thread(target=_remind_loop, args=(server,), daemon=True).start()
    if module_queue.backlog_settings(cfg):
        threading.Thread(target=_refresh_loop, args=(server,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        announce("dashboard stopped")
    finally:
        server.stopping.set()
        server.server_close()


class _Handler(BaseHTTPRequestHandler):
    server: DashboardServer
    server_version = "agentflow"
    sys_version = ""

    # -- logging: never the token
    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        if isinstance(code, int) and code >= 400:
            self.log_message("%s %s -> %s", self.command, urlsplit(getattr(self, "path", "")).path, code)

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("dashboard: " + (format % args).replace(self.server.token, "<token>") + "\n")

    # -- responses
    def _send(self, status: int, body: bytes, ctype: str, csp: str = API_CSP) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers(csp)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _security_headers(self, csp: str) -> None:
        self.send_header("Content-Security-Policy", csp)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.close_connection = True
        self._send(code, b"", "text/plain; charset=utf-8")

    def _json(self, status: int, data: Any) -> None:
        self._send(status, _json_bytes(data), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    # -- checks
    def _same_site(self) -> bool:
        """The Host is this server's (no DNS rebinding) and an Origin, when sent, is the dashboard's own."""
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1 or hosts[0] not in self.server.hosts:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in self.server.origins

    def _allowed(self) -> bool:
        if not self._same_site():
            return False
        given = self.headers.get("X-Agentflow-Token")
        if given is None:
            values = parse_qs(urlsplit(self.path).query).get("token") or []
            given = values[0] if len(values) == 1 else None
        if not given:
            return False
        return hmac.compare_digest(given.encode("utf-8"), self.server.token.encode("utf-8"))

    def _run(self, text: str) -> Run | None:
        """The run named exactly `text`, or None (no partial matches, nothing outside the runs root)."""
        if len(text) > RUN_ID_MAX or not RUN_ID.fullmatch(text):
            return None
        root = runs_root(self.server.repo).resolve()
        folder = root / text
        if folder.is_symlink() or not (folder / "events.jsonl").is_file() or folder.resolve().parent != root:
            return None
        return Run.load(self.server.repo, text)

    # -- dispatch
    def do_GET(self) -> None:
        if urlsplit(self.path).path == "/":  # the shell carries no run data; it reads the token from #token=
            if self._same_site():
                self._page()
            else:
                self._send(403, b"", "text/plain; charset=utf-8")
            return
        self._handle(self._get)

    def do_POST(self) -> None:
        self._handle(self._post)

    def _handle(self, route: Callable[[str, dict], None]) -> None:
        try:
            if not self._allowed():
                self._send(403, b"", "text/plain; charset=utf-8")
                return
            parts = urlsplit(self.path)
            route(parts.path, parse_qs(parts.query))
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            sys.stderr.write("dashboard: " + traceback.format_exc().replace(self.server.token, "<token>"))
            self._error(500, "internal error; see the dashboard's terminal")

    def _get(self, path: str, query: dict) -> None:
        if path == "/api/board":
            lane = (query.get("lane") or [None])[0]
            if lane is not None and lane not in self.server.cfg.lanes:
                self._error(404, f"unknown lane {lane!r}")
                return
            self._json(200, board(self.server.repo, self.server.cfg, lane))
        elif path == "/api/workflow":
            self._json(200, workflow_view(self.server.cfg))
        elif path == "/api/agents":
            self._json(200, agents_view(self.server.repo, self.server.cfg))
        elif path == "/api/events":
            self._events()
        elif path == "/api/config":
            self._json(200, config_edit.form_fields(self.server.cfg.path))
        elif path == "/api/metrics":
            self._metrics(query)
        elif match := RUN_ROUTE.match(path):
            run = self._run(match.group(1))
            if run is None:
                self._error(404, "no such run")
                return
            self._json(200, run_detail(run, self.server.cfg))
        else:
            self._error(404, "not found")

    def _metrics(self, query: dict) -> None:
        lane = (query.get("lane") or [None])[0] or None
        if lane is not None and lane not in self.server.cfg.lanes:
            self._error(404, f"unknown lane {lane!r}")
            return
        try:
            days = {name: date.fromisoformat(v) if (v := (query.get(name) or [""])[0]) else None
                    for name in ("from", "to")}
            view = metrics_view(self.server.repo, self.server.cfg, lane, days["from"], days["to"],
                                all_time=(query.get("all") or [""])[0] in ("1", "true"))
        except ValueError as exc:
            self._error(400, f"bad date range: {exc}")
            return
        self._json(200, view)

    def _page(self) -> None:
        nonce = secrets.token_urlsafe(18)
        html = PAGE.read_text(encoding="utf-8").replace("{{NONCE}}", nonce)
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", PAGE_CSP.format(n=nonce))

    def _post(self, path: str, query: dict) -> None:
        if path in BACKLOG_ROUTES:
            self._backlog_post(path)
            return
        if path in CONFIG_ROUTES:
            self._config_post(path)
            return
        match = ACTION_ROUTE.match(path)
        run = self._run(match.group(1)) if match else None
        if match is None or run is None or match.group(2) not in (*ACTIONS, "approve"):
            self._error(404, "no such run or action")
            return
        body = self._body()
        if body is None:
            return
        try:
            action = match.group(2)
            result, go_on = _approve(run, body, self.server.cfg) if action == "approve" else ACTIONS[action](run, body)
        except CONFLICTS as exc:
            self._error(409, str(exc))
            return
        except (BadRequest, ValueError) as exc:
            self._error(400, str(exc))
            return
        resumed = False
        if go_on and lock_state(run.dir) != "live":  # a live runner picks the action up itself
            self.server.spawn(run)
            resumed = True
        self._json(200, {"ok": True, **result, "resumed": resumed})

    def _backlog_post(self, path: str) -> None:
        body = self._body()
        if body is None:
            return
        server = self.server
        try:
            if path == "/api/backlog":
                item = backlog.add(server.cfg, server.repo, _text(body, "lane", required=True) or "",
                                   body.get("fields") if "fields" in body else {}, body.get("after") or [],
                                   _text(body, "to_phase"))
                self._json(200, {"ok": True, "key": f"card:{item.id}", "id": item.id})
            elif path == "/api/backlog/start":
                key = self._start_key(body)
                job = backlog.begin_start(server.cfg, server.repo, key)
                thread = threading.Thread(target=self._start_worker, args=(job,), daemon=True)
                server.starts.append(thread)
                thread.start()
                self._json(202, {"ok": True, "starting": True, "key": key})
            else:
                kind, name = backlog.parse_key(_text(body, "item", required=True))
                if kind != "card":
                    raise backlog.Refused("a capability-map module lives in the map; it cannot be removed here")
                backlog.remove(server.cfg, server.repo, name)
                self._json(200, {"ok": True})
        except backlog.Refused as exc:
            self._error(409, str(exc))
        except (BadRequest, backlog.Invalid) as exc:
            self._error(400, str(exc))

    def _config_post(self, path: str) -> None:
        body = self._body(CONFIG_MAX_BODY)
        if body is None:
            return
        server = self.server
        base, changes, text = body.get("base_sha256"), body.get("changes"), body.get("text")
        if not isinstance(base, str) or (changes is None) == (text is None):
            self._error(400, "send base_sha256 and either changes or text")
            return
        config_path = Path(server.cfg.path)
        try:
            proposal = config_edit.propose(config_path, base, changes=changes, text=text, repo=server.repo)
            if path == "/api/config/preview":
                self._json(200, proposal.as_dict())
                return
            if proposal.errors:
                self._json(400, {**proposal.as_dict(), "error": "the change does not validate"})
                return
            digest = config_edit.save(config_path, proposal) if proposal.changed else proposal.base_sha256
        except config_edit.Conflict as exc:
            self._error(409, str(exc))
            return
        except config_edit.EditError as exc:
            self._error(400, str(exc))
            return
        try:
            server.cfg = load_config(config_path)
        except ConfigError as exc:  # validated a moment ago; only a concurrent editor gets here
            self._error(500, f"saved, but the dashboard could not reload it: {exc}")
            return
        self.log_message("config.yaml saved from the Settings page (sha256 %s)", digest[:12])
        self._json(200, {**proposal.as_dict(), "ok": True, "saved": proposal.changed, "sha256": digest})

    @staticmethod
    def _start_key(body: dict) -> str:
        module, item = _text(body, "module"), _text(body, "item")
        if (module is None) == (item is None):
            raise BadRequest("name exactly one of module and item")
        return f"module:{module}" if module is not None else str(item)

    def _start_worker(self, job: backlog.StartJob) -> None:
        server = self.server
        try:
            backlog.run_start(server.cfg, server.repo, job, spawn=server.spawn_run, config=server.config)
        except Exception:  # run_start has recorded the failure; the page shows it on the card
            sys.stderr.write("dashboard: " + traceback.format_exc().replace(server.token, "<token>"))

    def _body(self, max_body: int = MAX_BODY) -> dict | None:
        """The request's JSON object, or None after answering the error."""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self.close_connection = True
            self._error(415, "send application/json")
            return None
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self.close_connection = True
            self._error(411, "Content-Length is required")
            return None
        try:
            length = int(raw_length)
        except ValueError:
            length = -1
        if length < 0 or length > max_body:
            self.close_connection = True
            self._error(413 if length > max_body else 400, f"the body must be at most {max_body} bytes")
            return None
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, UnicodeDecodeError):
            body = None
        if not isinstance(body, dict):
            self._error(400, "the body must be a JSON object")
            return None
        return body

    # -- server-sent events
    def _events(self) -> None:
        # The starting point is fixed before the 200 goes out: a client that acts as soon as it sees the
        # response must get the events it causes, so nothing written after this line counts as old.
        root = runs_root(self.server.repo)
        offsets = {path.parent.name: self._size(path) for path in self._logs(root)}
        offsets[BACKLOG_LOG] = self._size(root / "backlog.jsonl")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self._security_headers(API_CSP)
        self.end_headers()
        self.close_connection = True
        self.wfile.write(b": connected\n\n")
        self.wfile.flush()
        pinged = time.monotonic()
        while not self.server.stopping.is_set():
            sent = False
            for path in self._logs(root):
                name = path.parent.name
                for event in self._new_events(path, offsets, name):
                    self.wfile.write(b"data: " + _json_bytes({"run_id": name, "event": event}) + b"\n\n")
                    sent = True
            for event in self._new_events(root / "backlog.jsonl", offsets, BACKLOG_LOG):
                self.wfile.write(b"data: " + _json_bytes({"run_id": BACKLOG_LOG, "event": event}) + b"\n\n")
                sent = True
            if not sent and time.monotonic() - pinged > SSE_PING_S:
                self.wfile.write(b": ping\n\n")
                sent = True
            if sent:
                self.wfile.flush()
                pinged = time.monotonic()
            self.server.stopping.wait(SSE_POLL_S)

    @staticmethod
    def _size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    @staticmethod
    def _logs(root: Path) -> list[Path]:
        if not root.is_dir():
            return []
        return [p for p in sorted(root.glob("*/events.jsonl"))
                if RUN_ID.fullmatch(p.parent.name) and not p.parent.is_symlink() and not p.is_symlink()]

    @staticmethod
    def _new_events(path: Path, offsets: dict[str, int], name: str) -> list[dict]:
        start = offsets.get(name, 0)
        try:
            size = path.stat().st_size
            if size < start:
                start = 0  # the log was replaced
            if size == start:
                return []
            with path.open("rb") as handle:
                handle.seek(start)
                chunk = handle.read(min(size - start, 1024 * 1024))
        except OSError:
            return []
        complete = chunk.rfind(b"\n") + 1
        offsets[name] = start + complete
        events = []
        for raw in chunk[:complete].split(b"\n"):
            try:
                event = json.loads(raw) if raw.strip() else None
            except ValueError:
                event = None
            if isinstance(event, dict):
                events.append(event)
        return events
