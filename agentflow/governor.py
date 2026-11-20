"""The usage-window governor: a verdict over Claude's 5-hour and 7-day windows, and the machine-wide file it reads.

The file is `${XDG_STATE_HOME:-~/.local/state}/agentflow/usage_window.json` (`window_path`), with a sibling
`usage_window.lock`; it is never under a pack's `paths.state_dir`, and it holds `"version": 1`. It is a scheduling
aid, not a security boundary: any process of the developer's account can write it, a run's build agent included, so
everything read from it is checked and a bad entry governs nothing.

- **Above is strictly greater.** A window governs when its utilization is above a mark (`> mark`).
- **Refusal.** A reading is `hard` when its utilization is above the hard mark or it is `refused` (the event's
  top-level status was `rejected` and named this window).
- **Valid readings.** `{utilization, resets_at, refused, ts}` is kept only when `utilization` is a finite number of
  at least 0 (above 1.5 is clamped to 1.5, so an overage still governs), `resets_at` is a whole number with
  `now - SLACK_S < resets_at <= now + span + SLACK_S`, and `refused` is a bool. Anything else is dropped when it is
  recorded and when it is read, so a planted far-future reading governs nothing. A reading governs until
  `resets_at + SLACK_S`, so a machine where every run is held still wakes after the reset.
- **Accounts.** Readings are kept per account (`windows: {account: {window: reading}}`); the key is a hash of the
  effective `CLAUDE_CONFIG_DIR` (`account_of`). A run, the queue and the Backlog read only their own account's.
- **Merging.** A reading for a later period replaces the stored one, an earlier period's is ignored; in the same
  period the higher utilization wins and `refused` sticks.
- **Failing open.** A missing, broken, oversized, symlinked or other-version file reads as empty (with `error` set
  when it exists), and a lock not taken within `LOCK_WAIT_S` skips the write. A file of another integer version is
  never written over. A broken or unversioned file is replaced only by a write that brings readings (`record`,
  `exit_session`); a heartbeat (`wait`, `unwait`, `enter_session`) never rewrites it. A hardlinked or non-regular
  lock file fails open. Writes go to an exclusive `usage_window.<uuid>.tmp` and are renamed in place under the
  lock; reads take no lock. No method of `WindowFile` raises.
- **Finish first.** A held run keeps a waiter entry `{pid, run_dir, rank, since, seen, ready, ready_since}`. It
  counts (`ahead`) only when it is `ready` (a bool), its times are finite and none is above `now + SLACK_S`, `seen`
  is within `FRESH_S`, `ready_since` within `READY_MAX_S`, `rank` is two finite numbers in `[0, 1] x [0, 10**6]`,
  and `pid` is an int with `1 < pid < 2**22` that is alive. A run waits only behind counting entries that rank
  ahead: the higher phase fraction, then the fewer open tasks, then the earlier `since`. A write prunes the entries
  that are malformed, dead, future-dated or no longer heartbeating; a live ready entry past `READY_MAX_S` stays (it
  only stops counting), so its owner keeps its first `ready_since`.
- **The interactive note.** `enter_session` registers a session and remembers whether any other live session of
  the account was open (`alone`, a session counting while its pid lives and for `SESSION_MAX_S`) and the stored
  readings (`baseline`). `exit_session` sets `interactive` when the session was alone and its first reading is in
  the baseline's period and at least `INTERACTIVE_RISE` above it.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import stat
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from agentflow import config, events, pack, run_env, safeio
from agentflow.config import Config, Usage

VERSION = 1
USAGE_HOLD = "usage window"  # the `run_held` reason
GOVERNOR_STEP = "governor"  # the hold key's step at a phase boundary: `<phase>/governor`
POLL_S = 30.0
SLACK_S = 60.0
LOCK_WAIT_S = 2.0
FRESH_S = 90.0  # a waiter's heartbeat must be this recent
READY_MAX_S = 600.0  # a ready waiter stops counting after this long
INTERACTIVE_RISE = 0.05
SESSION_MAX_S = 6 * 3600
SPANS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
MAX_BYTES = 256 * 1024

_UTILIZATION_MAX = 1.5
_OPEN_TASKS_MAX = 10**6
_PID_MAX = 2**22
_LABELS = {"five_hour": "5h", "seven_day": "7d"}
_NOTE_NUMBERS = ("rise", "from", "to", "since", "at", "resets_at")


def window_path() -> Path:
    return pack.user_state_root() / "usage_window.json"


@dataclass(frozen=True)
class Verdict:
    level: str  # "clear" | "soft" | "hard"
    window: str | None = None
    utilization: float | None = None
    until: float | None = None  # the governing reading's `resets_at + SLACK_S`


@dataclass(frozen=True)
class SessionTicket:
    token: str
    account: str
    alone: bool
    baseline: dict = field(default_factory=dict)


def _num(value: Any) -> float | None:
    """`value` as a finite float, or None for anything else (a bool, a string, NaN, an infinity)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _pid_ok(pid: Any) -> bool:
    return isinstance(pid, int) and not isinstance(pid, bool) and 1 < pid < _PID_MAX


def _rank(rank: Any) -> tuple[float, float] | None:
    if not isinstance(rank, list) or len(rank) != 2:
        return None
    phase, tasks = _num(rank[0]), _num(rank[1])
    if phase is None or tasks is None or not 0 <= phase <= 1 or not 0 <= tasks <= _OPEN_TASKS_MAX:
        return None
    return phase, tasks


def _alive(alive: Callable[[int], bool], pid: int) -> bool:
    try:
        return bool(alive(pid))
    except Exception:
        return False


def valid_reading(window: str, reading: Any, now: float) -> dict | None:
    """`reading` normalised to `{utilization, resets_at, refused, ts}`, or None when it fails a check (module doc).
    A `ts` that is not a finite number becomes `now`."""
    try:
        span = SPANS.get(window) if isinstance(window, str) else None
        if span is None or not isinstance(reading, Mapping):
            return None
        utilization = _num(reading.get("utilization"))
        if utilization is None or utilization < 0:
            return None
        resets = reading.get("resets_at")
        if isinstance(resets, float):
            if not math.isfinite(resets) or not resets.is_integer():
                return None
            resets = int(resets)
        if isinstance(resets, bool) or not isinstance(resets, int):
            return None
        if not now - SLACK_S < resets <= now + span + SLACK_S:
            return None
        refused = reading.get("refused")
        if not isinstance(refused, bool):
            return None
        ts = _num(reading.get("ts"))
        return {"utilization": min(utilization, _UTILIZATION_MAX), "resets_at": resets, "refused": refused,
                "ts": float(now) if ts is None else ts}
    except Exception:
        return None


def _marks(usage: Usage, window: Any) -> tuple[float | None, float | None]:
    """`(soft, hard)` for `window`; the 7-day window has no hard mark (only a refusal makes it hard)."""
    if window == "five_hour":
        return _num(getattr(usage, "five_hour_soft", None)), _num(getattr(usage, "five_hour_hard", None))
    if window == "seven_day":
        return _num(getattr(usage, "seven_day_soft", None)), None
    return None, None


def verdict(usage: Usage, windows: Mapping[str, Mapping], now: float) -> Verdict:
    """The level `usage`'s marks give one account's `windows` at `now`. `off` is always `clear`; any mode other than
    `observe` and `off` counts as `on`. `hard` beats `soft`; between two windows at one level, the one further above
    its mark is named."""
    try:
        if getattr(usage, "governor", "on") == "off" or not isinstance(windows, Mapping):
            return Verdict("clear")
        best: tuple[tuple[int, float], str, dict] | None = None
        for window in SPANS:
            reading = valid_reading(window, windows.get(window), now)
            if reading is None:
                continue
            soft, hard = _marks(usage, window)
            utilization = reading["utilization"]
            if reading["refused"] or (hard is not None and utilization > hard):
                key = (2, utilization - (1.0 if hard is None else hard))
            elif soft is not None and utilization > soft:
                key = (1, utilization - soft)
            else:
                continue
            if best is None or key > best[0]:
                best = (key, window, reading)
        if best is None:
            return Verdict("clear")
        (level, _), window, reading = best
        return Verdict("hard" if level == 2 else "soft", window, reading["utilization"], reading["resets_at"] + SLACK_S)
    except Exception:
        return Verdict("clear")


def merge_reading(old: Mapping | None, new: Mapping) -> dict:
    """`new` merged onto the stored `old`: a later period replaces it, an earlier one is ignored; in one period the
    higher utilization wins and `refused` sticks."""
    merged = dict(new)
    if not isinstance(old, Mapping):
        return merged
    try:
        if new["resets_at"] != old["resets_at"]:
            return merged if new["resets_at"] > old["resets_at"] else dict(old)
        merged["utilization"] = max(old["utilization"], new["utilization"])
        merged["refused"] = bool(old["refused"]) or bool(new["refused"])
    except (KeyError, TypeError):
        return dict(new)
    return merged


def _until(until: Any, now: Any) -> str:
    at, base = _num(until), _num(now)
    if at is None:
        return ""
    try:
        moment = datetime.fromtimestamp(at)
    except (OverflowError, OSError, ValueError):
        return ""
    return " until " + moment.strftime("%a %H:%M" if base is not None and at - base > 86400 else "%H:%M")


def describe(v: Verdict, usage: Usage, now: float) -> str:
    """One line for a hold or a refusal: `the 5h usage window is at 82% (above usage.five_hour_soft 0.75) until
    15:01`, or `the 5h usage window refuses requests until 15:01`. Never raises."""
    try:
        label = _LABELS.get(v.window) if isinstance(v.window, str) else None
        head = f"the {label} usage window" if label else "the usage window"
        if v.level == "clear":
            return f"{head} is clear"
        utilization = _num(v.utilization)
        soft, hard = _marks(usage, v.window)
        if v.level == "hard" and (hard is None or utilization is None or utilization <= hard):
            text = f"{head} refuses requests"
        else:
            name, mark = ("hard", hard) if v.level == "hard" else ("soft", soft)
            if utilization is None or mark is None or label is None:
                text = f"{head} is above its {name} mark"
            else:
                text = f"{head} is at {utilization:.0%} (above usage.{v.window}_{name} {mark:g})"
        return text + _until(v.until, now)
    except Exception:
        return "the usage window holds new work"


def rank_of(phases: list[str], phase: str, open_tasks: int | None) -> list[float]:
    """`[phase fraction, open tasks]`: `index / len(phases)` (0 for a phase not in `phases`, as the retro's), and the
    open tasks (`10**6` when unknown)."""
    try:
        order = list(phases)
        fraction = order.index(phase) / len(order) if phase in order else 0.0
    except Exception:
        fraction = 0.0
    ok = isinstance(open_tasks, int) and not isinstance(open_tasks, bool) and open_tasks >= 0
    return [float(fraction), float(min(open_tasks, _OPEN_TASKS_MAX) if ok else _OPEN_TASKS_MAX)]


def _counts(entry: Any, now: float, alive: Callable[[int], bool]) -> bool:
    """Whether the waiter `entry` counts at `now` (module doc, finish first)."""
    if not isinstance(entry, Mapping) or entry.get("ready") is not True:
        return False
    times = [_num(entry.get(key)) for key in ("seen", "since", "ready_since")]
    if any(t is None or t > now + SLACK_S for t in times):
        return False
    seen, _, ready_since = times
    if now - seen > FRESH_S or now - ready_since > READY_MAX_S or _rank(entry.get("rank")) is None:
        return False
    pid = entry.get("pid")
    return _pid_ok(pid) and _alive(alive, pid)


def _counting(waiting: Any, now: float, alive: Callable[[int], bool] | None) -> list[tuple[str, Mapping]]:
    if alive is None:
        alive = events._pid_alive
    if not isinstance(waiting, Mapping) or _num(now) is None:
        return []
    return [(key, entry) for key, entry in waiting.items() if isinstance(key, str) and _counts(entry, now, alive)]


def ahead(waiting: Mapping[str, Any], me: str, my_rank: list[float], my_since: float, now: float,
          alive: Callable[[int], bool] | None = None) -> list[str]:
    """The keys of the counting entries of `waiting` that rank ahead of `me`. Never raises: a bad entry is ignored."""
    try:
        mine = _rank(list(my_rank))
        if mine is None:
            return []
        since = _num(my_since)
        mine_key = (-mine[0], mine[1], now if since is None else since)
        out = []
        for key, entry in _counting(waiting, now, alive):
            phase, tasks = _rank(entry["rank"])  # type: ignore[misc]
            if key != me and (-phase, tasks, _num(entry["since"])) < mine_key:
                out.append(key)
        return out
    except Exception:
        return []


def account_key(config_dir: str | None) -> str:
    """The account a `CLAUDE_CONFIG_DIR` value names: 12 hex characters of its sha256 (`"default"` when unset)."""
    value = config_dir if isinstance(config_dir, str) and config_dir else "default"
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()[:12]


def account_of(cfg: Config) -> str:
    """The account `cfg`'s runs use: the effective `CLAUDE_CONFIG_DIR` of `run_env.base(cfg)` under `defaults.env`."""
    env = run_env.base(cfg)
    env.update(cfg.defaults.env or {})
    return account_key(env.get("CLAUDE_CONFIG_DIR"))


def current_usage(cfg: Config, last: Usage | None = None) -> Usage:
    """`usage` as the layers of `cfg` say now, or `last` (else `cfg.usage`) when the reload fails or `validate`
    reports an error under `usage`. Never raises."""
    fallback = last if last is not None else cfg.usage
    try:
        fresh = config.load_config(cfg.path)
        issues = config.validate(fresh, Path(cfg.path).parent.parent)
        if any(i.level == "error" and (i.path == "usage" or i.path.startswith("usage.")) for i in issues):
            return fallback
        return fresh.usage
    except Exception:
        return fallback


def _reject(name: str) -> None:
    raise ValueError(f"{name} is not JSON")


def _empty() -> dict:
    return {"windows": {}, "sessions": {}, "waiting": {}, "interactive": None}


def _windows(raw: Any, now: float) -> dict:
    out: dict[str, dict] = {}
    for account, readings in raw.items() if isinstance(raw, dict) else ():
        if isinstance(account, str) and account and isinstance(readings, dict):
            kept = {w: r for w in SPANS if (r := valid_reading(w, readings.get(w), now)) is not None}
            if kept:
                out[account] = kept
    return out


def _sessions(raw: Any) -> dict:
    out: dict[str, dict] = {}
    for token, s in raw.items() if isinstance(raw, dict) else ():
        if (isinstance(token, str) and isinstance(s, dict) and _pid_ok(s.get("pid")) and _num(s.get("since")) is not None
                and isinstance(s.get("run"), str) and isinstance(s.get("account"), str)):
            out[token] = {"pid": s["pid"], "run": s["run"], "since": _num(s["since"]), "account": s["account"]}
    return out


def _waiting(raw: Any) -> dict:
    out: dict[str, dict] = {}
    for key, e in raw.items() if isinstance(raw, dict) else ():
        if not isinstance(key, str) or not isinstance(e, dict):
            continue
        rank, since, seen, ready_since = _rank(e.get("rank")), _num(e.get("since")), _num(e.get("seen")), e.get("ready_since")
        if (not _pid_ok(e.get("pid")) or not isinstance(e.get("run_dir"), str) or rank is None or since is None
                or seen is None or not isinstance(e.get("ready"), bool)
                or (ready_since is not None and _num(ready_since) is None)):
            continue
        out[key] = {"pid": e["pid"], "run_dir": e["run_dir"], "rank": list(rank), "since": since, "seen": seen,
                    "ready": e["ready"], "ready_since": None if ready_since is None else _num(ready_since)}
    return out


def _note(raw: Any) -> dict | None:
    if not isinstance(raw, dict) or raw.get("window") not in SPANS:
        return None
    numbers = {key: _num(raw.get(key)) for key in _NOTE_NUMBERS}
    if any(n is None for n in numbers.values()):
        return None
    note = {"window": raw["window"], **numbers}
    if isinstance(raw.get("account"), str):
        note["account"] = raw["account"]
    return note


class WindowFile:
    """The machine-wide usage window file. No method raises: a failure leaves the file as it was."""

    def __init__(self, path: Path | None = None) -> None:
        try:
            self.path: Path | None = Path(path) if path is not None else window_path()
        except Exception:
            self.path = None

    # -- reading ---------------------------------------------------------------------------------------------------

    def _load(self, now: float) -> tuple[dict, str | None, bool]:
        """`(state, error, foreign)`: the file's valid entries; `error` when it exists but can't be used; `foreign`
        when it is another integer version, which is never written over."""
        state = _empty()
        if self.path is None:
            return state, "the user state folder can't be found", False
        try:
            data = safeio.read_bytes(self.path, MAX_BYTES)
        except (OSError, ValueError) as exc:
            return state, f"{self.path} can't be read ({exc})", False
        if data is None:
            return state, None, False
        try:
            raw = json.loads(data.decode("utf-8"), parse_constant=_reject)
        except (ValueError, RecursionError):
            return state, f"{self.path} is not valid JSON", False
        version = raw.get("version") if isinstance(raw, dict) else None
        if isinstance(version, bool) or not isinstance(version, int):
            return state, f"{self.path} has no version", False
        if version != VERSION:
            return state, f"{self.path} is version {version}, written by another engine", True
        state.update(windows=_windows(raw.get("windows"), now), sessions=_sessions(raw.get("sessions")),
                     waiting=_waiting(raw.get("waiting")), interactive=_note(raw.get("interactive")))
        return state, None, False

    def _snapshot(self, now: float) -> dict:
        try:
            state, error, _ = self._load(now)
            return {**state, "error": error}
        except Exception as exc:
            return {**_empty(), "error": f"the usage window file can't be read ({type(exc).__name__})"}

    def read(self) -> dict:
        """`{windows, sessions, waiting, interactive, error}`, holding only valid entries. It takes no lock."""
        return self._snapshot(time.time())

    # -- writing ---------------------------------------------------------------------------------------------------

    def _acquire(self, wait_s: float | None) -> TextIO | None:
        if self.path is None:
            return None
        wait = max(0.0, _num(LOCK_WAIT_S if wait_s is None else wait_s) or 0.0)
        try:
            handle = safeio.open_append(self.path.with_name(f"{self.path.stem}.lock"))
        except (OSError, ValueError):
            return None
        try:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:  # a hardlinked lock would contend with another
                handle.close()
                return None
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return handle
                except BlockingIOError:
                    pass
                left = deadline - time.monotonic()
                if left <= 0:
                    handle.close()
                    return None
                time.sleep(min(0.05, left))
        except BaseException:
            handle.close()
            raise

    @contextmanager
    def _locked(self, wait_s: float | None = None) -> Iterator[bool]:
        handle = self._acquire(wait_s)
        try:
            yield handle is not None
        finally:
            if handle is not None:
                handle.close()  # closing the only descriptor of this open releases the lock

    def _store(self, state: dict) -> None:
        """Write `state` to an exclusive temp file and rename it onto the file. Raises on failure (callers catch)."""
        assert self.path is not None
        text = json.dumps({"version": VERSION, **{k: state[k] for k in _empty()}}, allow_nan=False, sort_keys=True)
        if len(text) > MAX_BYTES:
            return  # a file `read` would refuse: keep the one there
        tmp = self.path.with_name(f"{self.path.stem}.{uuid.uuid4().hex}.tmp")
        created = False
        try:
            safeio.write_new(tmp, text + "\n", 0o600)
            created = True
            os.replace(tmp, self.path)
        except BaseException:
            if created:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            raise

    @staticmethod
    def _prune(state: dict, now: float) -> None:
        """Drop the sessions that no longer count and the waiters that are dead, future-dated or not heartbeating."""
        alive = events._pid_alive
        state["sessions"] = {k: s for k, s in state["sessions"].items()
                             if now - SESSION_MAX_S <= s["since"] <= now + SLACK_S and _alive(alive, s["pid"])}

        def live(e: dict) -> bool:
            times = [e["seen"], e["since"]] + ([] if e["ready_since"] is None else [e["ready_since"]])
            return (all(t <= now + SLACK_S for t in times) and now - e["seen"] <= FRESH_S
                    and (e["ready_since"] is not None or not e["ready"]) and _alive(alive, e["pid"]))

        state["waiting"] = {k: e for k, e in state["waiting"].items() if live(e)}

    @staticmethod
    def _merge_in(state: dict, account: Any, readings: Any, now: float) -> bool:
        """Merge the valid `readings` (stamped `ts = now`) into `account`'s; whether any was valid."""
        if not isinstance(account, str) or not account or not isinstance(readings, Mapping):
            return False
        brought = False
        for window in SPANS:
            raw = readings.get(window)
            reading = valid_reading(window, {**raw, "ts": now}, now) if isinstance(raw, Mapping) else None
            if reading is not None:
                stored = state["windows"].setdefault(account, {})
                stored[window] = merge_reading(stored.get(window), reading)
                brought = True
        return brought

    def record(self, account: str, readings: Mapping[str, Mapping], now: float, wait_s: float | None = None) -> None:
        """Merge `readings` (`{window: reading}`) into `account`'s, stamping `ts = now`. A lock not taken within
        `wait_s` (default `LOCK_WAIT_S`) skips the write."""
        try:
            with self._locked(wait_s) as held:
                if not held:
                    return
                state, error, foreign = self._load(now)
                brought = self._merge_in(state, account, readings, now)
                if foreign or (error and not brought):
                    return
                self._prune(state, now)
                self._store(state)
        except Exception:
            pass

    def enter_session(self, run_key: str, account: str, now: float) -> SessionTicket:
        """Register a session; the ticket says whether no other live session of `account` was open (`alone`) and
        holds the account's stored readings (`baseline`). On any failure the ticket is not `alone`."""
        token = f"{os.getpid()}:{uuid.uuid4().hex}"
        ticket = SessionTicket(token, str(account), False, {})
        try:
            with self._locked() as held:
                if not held:
                    return ticket
                state, error, _ = self._load(now)
                if error:
                    return ticket
                self._prune(state, now)
                alone = not any(s["account"] == account for s in state["sessions"].values())
                baseline = {w: dict(r) for w, r in state["windows"].get(account, {}).items()}
                state["sessions"][token] = {"pid": os.getpid(), "run": str(run_key), "since": float(now),
                                            "account": str(account)}
                self._store(state)
                ticket = SessionTicket(token, str(account), alone, baseline)
        except Exception:
            pass
        return ticket

    @staticmethod
    def _note_for(ticket: SessionTicket, first: Any, now: float) -> dict | None:
        baseline = ticket.baseline if isinstance(ticket.baseline, Mapping) else {}
        first = first if isinstance(first, Mapping) else {}
        best: dict | None = None
        for window in SPANS:
            base = valid_reading(window, baseline.get(window), now)
            head = valid_reading(window, first.get(window), now)
            if base is None or head is None or base["resets_at"] != head["resets_at"]:
                continue
            rise = head["utilization"] - base["utilization"]
            if rise >= INTERACTIVE_RISE and (best is None or rise > best["rise"]):
                best = {"window": window, "rise": rise, "from": base["utilization"], "to": head["utilization"],
                        "since": base["ts"], "at": head["ts"], "resets_at": float(head["resets_at"]),
                        "account": ticket.account}
        return best

    def exit_session(self, ticket: SessionTicket, first: Mapping, last: Mapping, now: float) -> None:
        """In one lock: record `last` (as `record` does), set the interactive note (module doc), drop the session."""
        try:
            with self._locked() as held:
                if not held:
                    return
                state, error, foreign = self._load(now)
                brought = self._merge_in(state, ticket.account, last, now)
                if foreign or (error and not brought):
                    return
                if ticket.alone is True:
                    note = self._note_for(ticket, first, now)
                    if note is not None:
                        state["interactive"] = note
                state["sessions"].pop(ticket.token, None)
                self._prune(state, now)
                self._store(state)
        except Exception:
            pass

    def wait(self, run_key: str, run_dir: str, rank: list[float], since: float, ready: bool, now: float) -> None:
        """Keep `run_key`'s waiter entry (the heartbeat): `seen = now`; `ready_since = now` when `ready` turns True,
        cleared when it turns False. Writes nothing on a file `read()` reports as an error."""
        try:
            with self._locked() as held:
                if not held:
                    return
                state, error, _ = self._load(now)
                if error:
                    return
                self._prune(state, now)
                prev = state["waiting"].get(run_key)
                ready = ready is True
                ready_since = None
                if ready:
                    kept = prev is not None and prev["ready"] and prev["ready_since"] is not None
                    ready_since = prev["ready_since"] if kept else float(now)
                state["waiting"][str(run_key)] = {
                    "pid": os.getpid(), "run_dir": str(run_dir), "rank": [float(rank[0]), float(rank[1])],
                    "since": float(since), "seen": float(now), "ready": ready, "ready_since": ready_since}
                self._store(state)
        except Exception:
            pass

    def unwait(self, run_key: str) -> None:
        """Drop `run_key`'s waiter entry. Writes nothing on a file `read()` reports as an error."""
        try:
            now = time.time()
            with self._locked() as held:
                if not held:
                    return
                state, error, _ = self._load(now)
                if error or state["waiting"].pop(run_key, None) is None:
                    return
                self._prune(state, now)
                self._store(state)
        except Exception:
            pass


def start_gate(usage: Usage, account: str, now: float | None = None, file: WindowFile | None = None) -> str | None:
    """Why a new run of `account` should not start now, or None. Only in `on` mode: the verdict's `describe` when it is
    `soft` or `hard`; with `finish_first`, the count of runs waiting to go first. Never raises."""
    try:
        if getattr(usage, "governor", "on") in ("observe", "off"):
            return None
        now = time.time() if now is None else now
        data = (file if file is not None else WindowFile())._snapshot(now)
        windows = data["windows"].get(account, {})
        v = verdict(usage, windows, now)
        if v.level in ("soft", "hard"):
            return describe(v, usage, now)
        if usage.finish_first:
            count = len(_counting(data["waiting"], now, None))
            if count:
                return f"{count} run(s) wait on the usage window and go first"
        return None
    except Exception:
        return None
