#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""agentflow: run the lanes of the repository's pack (.agentflow/config.yaml) headlessly.

Usage: agentflow <command>   (see `--help`; `bin/agentflow` runs this file)
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import traceback
from pathlib import Path

# The repo root goes on sys.path so `agentflow.*` imports work when this file runs as a script.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def repo_root(start: Path) -> Path:
    """The main checkout containing `start`, even when `start` is inside a linked worktree."""
    from agentflow.pack import main_checkout

    return main_checkout(start)


def _find_repo() -> Path:
    """The main checkout holding the working directory: the one place the CLI looks for the pack."""
    from agentflow.config import ConfigError

    try:
        return repo_root(Path.cwd())
    except (subprocess.CalledProcessError, OSError):
        raise ConfigError("", "not inside a git checkout") from None


def _pack_config() -> Path:
    """The repository's pack: `<main checkout>/.agentflow/config.yaml` (`PackError` when there is none)."""
    from agentflow.pack import pack_config

    return pack_config(_find_repo())


def cmd_validate(args: argparse.Namespace) -> int:
    from agentflow.config import ConfigError, load_config, validate
    from agentflow.guards import settings_coverage
    from agentflow.pack import PackError

    try:
        repo = _find_repo()
        config_path = Path(args.config) if args.config else _pack_config()
        cfg = load_config(config_path)
    except ConfigError as exc:
        where = f"{exc.path}: " if exc.path else ""
        print(f"error {where}{exc.message}")
        return 1
    except PackError as exc:
        print(f"error {exc}")
        return 1
    issues = validate(cfg, repo) + settings_coverage(cfg, repo)
    for issue in issues:
        print(f"{issue.level} {issue.path}: {issue.message}")
    errors = sum(1 for i in issues if i.level == "error")
    if errors:
        return 1
    print(f"ok: {config_path.name} is valid ({len(issues)} warnings)")
    return 0


def cmd_config_show(args: argparse.Namespace) -> int:
    """The pack file as written, or (`--effective`) every leaf of the layered config with the layer it came from."""
    from agentflow.config import ConfigError, load_config
    from agentflow.layers import rows
    from agentflow.pack import PackError

    try:
        config_path = Path(args.config) if args.config else _pack_config()
        if not args.effective:
            sys.stdout.write(config_path.read_text(encoding="utf-8"))
            return 0
        cfg = load_config(config_path)
    except ConfigError as exc:
        where = f"{exc.path}: " if exc.path else ""
        print(f"error {where}{exc.message}")
        return 1
    except (PackError, OSError) as exc:
        print(f"error {exc}")
        return 1
    prefix = args.effective if isinstance(args.effective, str) else ""
    for dotted, value, layer in rows(cfg.merged, cfg.layer_origins):
        if prefix and dotted != prefix and not dotted.startswith(prefix + "."):
            continue
        print(f"{dotted}: {json.dumps(value, ensure_ascii=False, default=str)}  [{layer}]")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check the tools and logins a run needs; exit 1 when any check fails."""
    from agentflow import doctor
    from agentflow.config import ConfigError
    from agentflow.pack import PackError

    try:
        repo = _find_repo()
        config_path = Path(args.config) if args.config else _pack_config()
        cfg, extra = doctor.load_checked(config_path)
    except ConfigError as exc:
        where = f"{exc.path}: " if exc.path else ""
        print(f"error {where}{exc.message}")
        return 1
    except (PackError, RuntimeError) as exc:
        print(f"error {exc}")
        return 1
    checks = [*extra, *doctor.run_checks(cfg, repo)]
    print(doctor.render(checks))
    return 1 if any(c.status == "fail" for c in checks) else 0


def cmd_init(args: argparse.Namespace) -> int:
    """Write a sparse pack for this repository; with `--baseline`, run its gates once on the base branch first."""
    from agentflow import doctor, init
    from agentflow.config import ConfigError

    try:
        repo = _find_repo()
        here = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=Path.cwd(), capture_output=True, text=True)
        if here.returncode == 0 and here.stdout.strip() and Path(here.stdout.strip()).resolve() != repo:
            print(f"init: this is a linked worktree; the pack goes into the main checkout, {repo}")
        return init.init(repo, starter=args.starter, run_baseline=args.baseline, out=sys.stdout)
    except ConfigError as exc:
        print(f"error: init: {doctor.printable(exc.message)}")
    except Exception as exc:  # init's own failure, never a run's: say so, and that no run is involved
        print(f"error: init stopped on an unexpected {type(exc).__name__}: {doctor.printable(exc)}")
    return 1


def _load_cfg(args: argparse.Namespace):
    from agentflow.config import load_config

    return load_config(Path(args.config) if getattr(args, "config", None) else _pack_config())


def _print_retro_due(cfg, repo: Path) -> None:
    """One line when a retro is due (`retro.due`); nothing otherwise, and never an error."""
    from datetime import UTC, datetime

    from agentflow.retro import due_or_none

    reason = due_or_none(repo, cfg, datetime.now(UTC))
    if reason:
        print(f"Retro due: {reason} → agentflow retro")


def _by(args: argparse.Namespace) -> str:
    return args.by or getpass.getuser()


def _human_errors() -> tuple[type[Exception], ...]:
    from agentflow.config import ConfigError
    from agentflow.events import EventError
    from agentflow.gitops import GhError, GitError
    from agentflow.hitl import NotWaiting
    from agentflow.pack import PackError
    from agentflow.pipeline import RunLocked
    from agentflow.run_control import ControlError
    from agentflow.state import RunNotFound

    return (RunNotFound, NotWaiting, EventError, ConfigError, PackError, ValueError, GitError, GhError, RunLocked,
            ControlError)


def _money(value: float | None) -> str:
    return "-" if value is None else f"${value:.2f}"


def _spend(state: dict) -> str:
    """The run's spend: finished sessions plus the live cost of sessions still running (as the dashboard shows),
    then `/ soft · hard` budget, or `/ budget` for a run started before the soft budget."""
    budget, soft = state["budget_usd"], state.get("soft_budget_usd")
    live = sum(float(row.get("spend") or 0.0) for row in (state.get("agents") or {}).values())
    limits = (f" / {_money(soft)} · {_money(budget)}" if soft and budget else f" / {_money(budget)}" if budget else "")
    return _money(float(state["spend_usd"] or 0.0) + live) + limits


def _table(rows: list[list[str]]) -> list[str]:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return [("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))).rstrip() for row in rows]


def _print_run_detail(run) -> None:
    from agentflow.state import display_status

    st = run.state
    print(f"run:      {run.id}")
    print(f"lane: {st['pipeline']}  params: {json.dumps(st['params'], ensure_ascii=False)}")
    print(f"status:   {display_status(run)}")
    task = f"  task {st['task']['n']}/{st['task']['of']}" if st["task"] else ""
    print(f"where:    phase {st['phase'] or '-'}  step {st['step'] or '-'}{task}")
    print(f"spend:    {_spend(st)}")
    for label, value in (("branch", st["branch"]), ("worktree", st["worktree"]), ("pr", st["pr_url"])):
        if value:
            print(f"{label}:".ljust(10) + str(value))
    if st.get("hold"):
        print(f"held:     {st['hold']['reason']} until {st['hold']['until']}, step {st['hold']['key']}")
    if st["waiting"]:
        w = st["waiting"]
        print(f"waiting:  {w['kind']} {w['key']} since {w['since']}")
        if w["summary"]:
            print(f"          {w['summary']}")
    if st["refine"]:
        print(f"refine:   {st['refine']['phase']} round {st['refine']['round']}: {st['refine']['note']}")
    if st["failure"]:
        print(f"failure:  {st['failure']}")
    if st["steps"]:
        print("steps:")
        for key, row in st["steps"].items():
            print(f"  {key}  {row['status']}")


def cmd_status(args: argparse.Namespace) -> int:
    from agentflow.state import display_status, find_run, list_runs

    if args.watch:
        from agentflow.views import board, board_table

        return _watch(args, lambda cfg, repo: board_table(board(repo, cfg)))
    try:
        repo = _find_repo()
        if args.run:
            _print_run_detail(find_run(repo, args.run))
            return 0
        runs = list_runs(repo)
    except _human_errors() as exc:
        print(f"error: {exc}")
        return 1
    if not runs:
        print("No runs yet.")
        return 0
    rows = [["RUN", "LANE", "PHASE", "STEP", "STATUS", "SPEND"]]
    for run in runs:
        st = run.state
        rows.append([run.id, str(st["pipeline"]), st["phase"] or "-", st["step"] or "-", display_status(run), _spend(st)])
    print("\n".join(_table(rows)))
    try:
        _print_retro_due(_load_cfg(args), repo)
    except _human_errors():  # status needs no pack: with none there is no reminder
        pass
    return 0


def cmd_inbox(args: argparse.Namespace) -> int:
    from agentflow.config import RETRO_PIPELINE
    from agentflow.hitl import inbox_items

    try:
        items = inbox_items(_find_repo())
    except _human_errors() as exc:
        print(f"error: {exc}")
        return 1
    if not items:
        print("Nothing is waiting on you.")
        return 0
    for item in items:
        print(f"{item['run_id']}  {item['kind']}  {item['key']}  since {item['since']}")
        if item["summary"]:
            print(f"  {item['summary']}")
        for q in item["questions"]:
            print(f"  {q['id']}: {q['question']}")
        if item["inbox_path"]:
            print(f"  file: {item['inbox_path']}")
        if item.get("pipeline") == RETRO_PIPELINE:  # a retro is never resumed: cancel it and run another
            print(f"  agentflow cancel {item['run_id']}    then    agentflow retro")
        elif item["kind"] == "phase_approval":
            print(f"  agentflow approve {item['run_id']} {item['phase']}    |    agentflow reject {item['run_id']} {item['phase']} --note \"...\"")
        elif item["kind"] == "parked":
            print(f"  agentflow answer {item['run_id']} --key {item['key']}")
        elif item["questions"]:
            print(f"  agentflow answer {item['run_id']}")
        else:
            print(f"  agentflow approve {item['run_id']} {item['key']}    |    agentflow reject {item['run_id']} {item['key']} --note \"...\"")
    return 0


def _parse_sets(pairs: list[str]) -> dict[str, str]:
    answers = {}
    for pair in pairs:
        qid, sep, value = pair.partition("=")
        if not sep or not qid.strip():
            raise ValueError(f"--set expects q1=answer, got {pair!r}")
        answers[qid.strip()] = value
    return answers


def _ask_interactively(run, key: str, waiting: dict) -> dict[str, str]:
    from agentflow.hitl import parse_answers

    path = run.inbox_path(key)
    done = parse_answers(path) if path.exists() else {}
    collected: dict[str, str] = {}
    for q in waiting["questions"]:
        if done.get(q["id"]):
            continue
        print(f"\n{q['id']}: {q['question']}")
        for ref in q.get("evidence", []):
            print(f"  see {ref}")
        for opt in q.get("options", []):
            print(f"  {opt['key']}: {opt['label']}{' (recommended)' if opt.get('recommended') else ''}")
        try:
            reply = input("Answer (an option key or free text, empty to skip): ")
        except EOFError:
            break
        if reply.strip():
            collected[q["id"]] = reply
    return collected


def cmd_answer(args: argparse.Namespace) -> int:
    from agentflow.hitl import NotWaiting, answer, parse_answers, pending_item
    from agentflow.state import find_run

    try:
        run = find_run(_find_repo(), args.run)
        waiting = pending_item(run, args.key)
        if waiting is None:
            raise NotWaiting(f"{run.id} is not waiting for answers" + (f" on {args.key!r}" if args.key else ""))
        answers = _parse_sets(args.set) if args.set else _ask_interactively(run, waiting["key"], waiting)
        complete = answer(run, waiting["key"], answers, _by(args), record=args.record)
        pending = [qid for qid, text in parse_answers(run.inbox_path(waiting["key"])).items() if not text]
    except _human_errors() as exc:
        print(f"error: {exc}")
        return 1
    if not complete:
        print(f"Recorded, still waiting on: {', '.join(pending)}.")
        return 0
    print(f"Answers recorded for {run.id} {waiting['key']}.")
    try:
        return _after_answer(run, waiting["key"], args)
    except _human_errors() as exc:
        print(f"error: {exc}")
        return 1


def _after_answer(run, key: str, args: argparse.Namespace) -> int:
    """D13: continue the run; when the answered checkpoint ends its phase, offer that phase's approval."""
    from agentflow.hitl import approve
    from agentflow.pipeline import RunOptions, advance

    if args.no_continue:
        return _continue(run, args)
    cfg = _run_cfg(run, args)
    offer = _approval_offer(cfg, run, key)
    if offer is None:
        return _continue(run, args, cfg)
    try:
        approve_now = input(f"Approve {offer} now? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        approve_now = False
    if not approve_now:
        return _continue(run, args, cfg)
    problem = _preflight(cfg)
    if problem:
        print(f"error: {problem}")
        return 1
    status = advance(run, cfg, RunOptions())  # clears the checkpoint and raises the approval; no agent runs
    run.refresh()
    waiting = run.state["waiting"]
    if status != "awaiting_approval" or waiting is None or waiting.get("phase") != offer:
        _report(run, status)
        return 0
    approve(run, offer, _by(args))
    print(f"approved {offer} for {run.id}.")
    return _continue(run, args, cfg)


def _approval_offer(cfg, run, key: str) -> str | None:
    """The phase to offer approving when `key` is an answers checkpoint that ends a phase with an approval."""
    from agentflow.pipeline import needs_approval

    name = key.split("/")[0]
    phase = cfg.phases.get(name)
    if phase is None or not phase.steps:
        return None
    last = phase.steps[-1]
    if last.type != "checkpoint" or key != f"{name}/{last.id}":
        return None
    return name if needs_approval(phase) else None


def cmd_approve(args: argparse.Namespace) -> int:
    from agentflow.config import ConfigError
    from agentflow.hitl import approve
    from agentflow.pack import PackError
    from agentflow.state import find_run

    try:
        run = find_run(_find_repo(), args.run)
        cfg = None
        if args.budget is None and (run.state["waiting"] or {}).get("kind") == "budget_exceeded":
            try:  # a plain approve of the hard budget adds one soft budget, the pack's `run_budget_usd`
                cfg = _run_cfg(run, args)
            except (ConfigError, PackError):
                cfg = None  # the run's own soft budget instead; continuing reports the config error
        approve(run, args.target, _by(args), budget=args.budget, hold=args.no_continue, note=args.note,
                budget_step=cfg.defaults.run_budget_usd if cfg is not None else None)
        print(f"approved {args.target} for {run.id}.")
        return _continue(run, args, cfg)
    except _human_errors() as exc:
        print(f"error: {exc}")
        return 1


def cmd_reject(args: argparse.Namespace) -> int:
    from agentflow.hitl import reject
    from agentflow.state import find_run

    try:
        run = find_run(_find_repo(), args.run)
        reject(run, args.target, _by(args), args.note)
        print(f"rejected {args.target} for {run.id}: refine round {run.state['refine']['round']} recorded.")
        return _continue(run, args)
    except _human_errors() as exc:
        print(f"error: {exc}")
        return 1


# --------------------------------------------------------------------------- running


PARAM_FLAGS = ("module", "topic", "issue", "log", "task", "slice")


def _run_cfg(run, args: argparse.Namespace):
    """The config a run continues with: `--config`, else the one it was started with when that is still
    trusted (`config_trust`), else the repository's pack."""
    from agentflow.config import load_config
    from agentflow.config_trust import run_config_path

    return load_config(run_config_path(run, getattr(args, "config", None)))


def _preflight(cfg) -> str | None:
    """A message when an enabled agent's command cannot be found, before anything is created."""
    for name, spec in cfg.agents.items():
        if spec.enabled and shutil.which(spec.command[0]) is None:
            return (f"agent {name!r}: command {spec.command[0]!r} was not found on PATH "
                    f"(agents.{name}.command in {cfg.path})")
    return None


def _report(run, status: str) -> None:
    run.refresh()
    waiting = run.state["waiting"]
    if status == "awaiting_approval" and waiting:
        phase = waiting["phase"]
        print(f"{run.id} is waiting for {waiting['key']}. Review: {run.inbox_path(waiting['key'])}")
        print(f"  agentflow approve {run.id} {phase}    |    agentflow reject {run.id} {phase} --note \"...\"")
    elif status == "waiting_human" and waiting:
        print(f"{run.id} is waiting on {waiting['key']} ({waiting['kind']}): {waiting['summary']}")
        how = f"agentflow answer {run.id}" if waiting["questions"] else f"agentflow approve {run.id} {waiting['key']}"
        print(f"  see {run.inbox_path(waiting['key'])}, then `{how}`")
    elif status == "done":
        print(f"{run.id} is done." + (f" Pull request: {run.state['pr_url']}" if run.state["pr_url"] else ""))
    else:
        print(f"{run.id} is {status}.")


def _spawn_resume(run, args: argparse.Namespace) -> None:
    """Continue the run in a background process (`--detach`), logging to `runs/<run>/runner.log`."""
    from agentflow.run_control import spawn_resume

    log = spawn_resume(run, getattr(args, "config", None))
    print(f"{run.id} goes on in the background; log: {log}")


def _survive_hangup(run) -> None:
    """Keep a foreground runner going when its terminal closes, with its output in `runs/<run>/runner.log`.

    Agents run in their own sessions, so a runner killed by the hangup would leave one working unwatched
    and outside the budget."""
    from agentflow import safeio

    def hangup(_signum, _frame) -> None:
        with safeio.open_append(run.dir / "runner.log") as log:
            os.dup2(log.fileno(), 1)
            os.dup2(log.fileno(), 2)

    signal.signal(signal.SIGHUP, hangup)


def _continue(run, args: argparse.Namespace, cfg=None) -> int:
    """D13: after a human action, run on in the foreground (`--detach`: background; `--no-continue`: stop)."""
    from agentflow.pipeline import RunOptions, advance

    if getattr(args, "no_continue", False):
        print(f"Continue the run with `agentflow resume {run.id}`.")
        if run.state["status"] == "ready":
            print(f"Or finish it here with `agentflow resume {run.id} --mark-done`.")
        return 0
    cfg = cfg or _run_cfg(run, args)
    problem = _preflight(cfg)
    if problem:
        print(f"error: {problem}")
        return 1
    if getattr(args, "detach", False):
        _spawn_resume(run, args)
        return 0
    _note_changed_layers(run, cfg)
    _survive_hangup(run)
    _report(run, advance(run, cfg, RunOptions()))
    return 0


def _note_changed_layers(run, cfg) -> None:
    """One line naming the config layer files that changed since the run started: the run goes on with the current
    files (`config_trust` decides which pack file is trusted, by its own hash)."""
    from agentflow.config_trust import changed_layers

    changed = changed_layers(run, cfg)
    if changed:
        print(f"note: config layer files changed since {run.id} started: {', '.join(map(str, changed))}; "
              "the run goes on with the files as they are now.")


def _parse_new_module(text: str | None) -> dict | None:
    """`--new-module`: a JSON object of exactly `module` (a slug), `responsibility` (text) and `deps` (slugs).
    It is checked as a shape and only ever reaches a markdown file, never a shell."""
    if text is None:
        return None
    from agentflow.module_queue import _MODULE_ID

    def bad(why: str) -> ValueError:
        return ValueError(f"--new-module {why}")

    try:
        data = json.loads(text)
    except ValueError as exc:
        raise bad(f"is not JSON: {exc}") from exc
    if not isinstance(data, dict) or set(data) != {"module", "responsibility", "deps"}:
        raise bad("must be an object with exactly module, responsibility and deps")
    deps = data["deps"]
    if not isinstance(data["module"], str) or not _MODULE_ID.fullmatch(data["module"]):
        raise bad("module must be a lowercase slug")
    if not isinstance(data["responsibility"], str) or not data["responsibility"].strip() or "\0" in data["responsibility"]:
        raise bad("responsibility must be non-empty text")
    if not isinstance(deps, list) or not all(isinstance(d, str) and _MODULE_ID.fullmatch(d) for d in deps):
        raise bad("deps must be a list of module slugs")
    return data


def cmd_run(args: argparse.Namespace) -> int:
    from agentflow.pipeline import RunOptions, advance, dry_run, start

    params = {flag: getattr(args, flag) for flag in PARAM_FLAGS if getattr(args, flag)}
    try:
        new_module = _parse_new_module(args.new_module)
        if not params:
            raise ValueError("run needs one of --module, --topic, --issue, --log or --task")
        cfg = _load_cfg(args)
        opts = RunOptions(from_phase=args.from_phase, to_phase=args.to_phase,
                          model_overrides=_parse_models(args.model, cfg.phases), dry_run=args.dry_run,
                          detach=args.detach, new_module=new_module)
        repo = _find_repo()
        if args.dry_run:
            dry_run(cfg, repo, args.lane, params, opts)
            return 0
        problem = _preflight(cfg)
        if problem:
            print(f"error: {problem}")
            return 1
        run = start(cfg, repo, args.lane, params, opts)
        print(f"Started {run.id} in {run.state['worktree']} on {run.state['branch']}.")
        if args.detach:
            _spawn_resume(run, args)
            return 0
        _survive_hangup(run)
        _report(run, advance(run, cfg, opts))
    except _human_errors() as exc:
        print(f"error: {exc}")
        return 1
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    from agentflow.hitl import refuse_retro
    from agentflow.run_control import mark_done
    from agentflow.state import find_run

    try:
        run = find_run(_find_repo(), args.run)
        refuse_retro(run)
        if args.mark_done:
            mark_done(run, _by(args))
        return _continue(run, args)
    except _human_errors() as exc:
        print(f"error: {exc}")
        return 1


def _parse_models(pairs: list[str], phases: dict) -> dict[str, str]:
    overrides = {}
    for pair in pairs:
        phase, sep, model = pair.partition("=")
        if not sep or not model.strip():
            raise ValueError(f"--model expects <phase>=<model>, got {pair!r}")
        if phase.strip() not in phases:
            raise ValueError(f"--model names an unknown phase {phase.strip()!r}; known: {', '.join(sorted(phases))}")
        overrides[phase.strip()] = model.strip()
    return overrides


_VERDICT_MARK = {"run": "\u2713 run", "skip": "\u2013 skip", "ask": "? ask"}


def _explain_row(line, indent: str) -> str:
    if line.verdict == "approval":
        return f"{indent}\u23f8 {line.key}"
    mark = _VERDICT_MARK[line.verdict]
    if line.key == line.phase:
        label = f"{line.phase}: {mark}"
    else:
        label = f"{mark}  {line.key.split('/', 1)[1]}"
    model = f"  [{line.model}]" if line.model else ""
    reason = f" ({line.reason})" if line.reason else ""
    return f"{indent}{label}{model}{reason}"


def cmd_explain(args: argparse.Namespace) -> int:
    from agentflow.conditions import explain, make_ctx, plan_warnings

    params = {k: v for k, v in (("module", args.module), ("topic", args.topic), ("issue", args.issue)) if v}
    try:
        if not params:
            raise ValueError("explain needs --module <id>, --topic <text> or --issue <ref>")
        cfg = _load_cfg(args)
        overrides = _parse_models(args.model, cfg.phases)
        ctx = make_ctx(cfg, _find_repo(), args.lane, params)
        lines = explain(cfg, args.lane, ctx, overrides)
        warnings = plan_warnings(ctx)
    except _human_errors() as exc:
        print(f"error: {exc}")
        return 1
    print(f"explain {args.lane} {json.dumps(params, ensure_ascii=False)}")
    for line in lines:
        print(_explain_row(line, "" if line.key == line.phase else "  "))
    for warning in warnings:
        print(f"warning: {warning}")
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    from agentflow.module_queue import format_answer, next_step

    try:
        cfg, repo = _load_cfg(args), _find_repo()
        print(format_answer(next_step(cfg, repo)))
        _print_retro_due(cfg, repo)
    except _human_errors() as exc:
        print(f"error: {exc}")
        return 1
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    from agentflow.module_queue import QueueBusy, module_lane, run_queue, spawn_feature_run

    try:
        if args.max_parallel < 1:
            raise ValueError(f"--max-parallel must be at least 1, got {args.max_parallel}")
        if args.interval <= 0:
            raise ValueError(f"--interval must be above 0, got {args.interval}")
        cfg = _load_cfg(args)
        repo = _find_repo()
        if cfg.work_source is not None and cfg.work_source.kind == "specs" and not args.include_specs:
            from agentflow import work_source

            # every spec file is a module here, finished or not: starting them all must be asked for
            print(f"error: this pack's work source is its spec files: {len(work_source.items(cfg, repo))} modules "
                  "are listed; pass --include-specs to queue them")
            return 1
        problem = _preflight(cfg)
        if problem:
            print(f"error: {problem}")
            return 1
        lane = module_lane(cfg)
        if lane is None:
            raise ValueError("dashboard.backlog.module_lane is not set in config.yaml: the queue does not know which lane runs a module")
        config = Path(args.config).resolve() if args.config else None
        return run_queue(cfg, repo, args.max_parallel,
                         lambda module: spawn_feature_run(repo, module, lane=lane, config=config),
                         once=args.once, interval=args.interval, log=lambda message: print(message, flush=True))
    except QueueBusy as exc:
        print(f"error: {exc}")
        return 1
    except _human_errors() as exc:
        print(f"error: {exc}")
        return 1


def cmd_retro(args: argparse.Namespace) -> int:
    """Learn from the runs that ended since the last retro (or `--since`): one agent session over a digest, then a
    draft pull request that changes only the lessons file. `--dry-run` prints the digest and the prompt and creates
    nothing. The exit code is 1 when the retro stopped for a human (or failed), 0 otherwise."""
    from datetime import UTC, datetime

    from agentflow.retro import run_retro
    from agentflow.state import Run

    try:
        cfg, repo = _load_cfg(args), _find_repo()
        if cfg.retro_step is None:
            print("error: this pack has no retro step (`retro_step: null`): remove that line to use `agentflow retro`")
            return 1
        if not args.dry_run:
            problem = _preflight(cfg)
            if problem:
                print(f"error: {problem}")
                return 1
        outcome = run_retro(repo, cfg, args.since, args.dry_run, datetime.now(UTC))
        print(outcome.message)
        if outcome.pr_url:
            print(f"Pull request: {outcome.pr_url}")
        if outcome.refused:
            return 1
        if outcome.run_id is None:
            return 0
        return 0 if Run.load(repo, outcome.run_id).state["status"] == "done" else 1
    except _human_errors() as exc:
        print(f"error: {exc}")
        return 1


def cmd_backlog(args: argparse.Namespace) -> int:
    """`backlog done <module>` marks a module finished by hand; `backlog undone <module>` takes that mark off, or
    (with none) vetoes the origin evidence that says it is done."""
    from agentflow import backlog

    act = backlog.mark_done if args.backlog_command == "done" else backlog.mark_not_done
    try:
        for line in act(_load_cfg(args), _find_repo(), args.module, _by(args)):
            print(line)
        return 0
    except (backlog.Refused, *_human_errors()) as exc:
        print(f"error: {exc}")
        return 1


# --------------------------------------------------------------------------- run control and live views


def _watch(args: argparse.Namespace, build) -> int:
    """Re-render `build(cfg, repo)` on every change to the runs, sending due reminders once a minute."""
    from agentflow.hitl import remind_due
    from agentflow.views import runs_signature, watch

    try:
        cfg = _load_cfg(args)
        repo = _find_repo()
        watch(lambda: build(cfg, repo), lambda: runs_signature(repo),
              out=lambda text: print("\x1b[H\x1b[2J" + text, flush=True), remind=lambda: remind_due(cfg, repo))
    except KeyboardInterrupt:
        return 0
    except _human_errors() as exc:
        print(f"error: {exc}")
        return 1
    return 0


def cmd_agents(args: argparse.Namespace) -> int:
    from agentflow.views import agents_table, agents_view

    if args.watch:
        return _watch(args, lambda cfg, repo: agents_table(agents_view(repo, cfg)))
    try:
        print(agents_table(agents_view(_find_repo(), _load_cfg(args))))
    except _human_errors() as exc:
        print(f"error: {exc}")
        return 1
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    """Time and cost by phase, bucket and model · effort: one run, or every run started in a date range."""
    import json as _json
    from datetime import date

    from agentflow.state import find_run
    from agentflow.views import metrics_csv, metrics_table, metrics_view

    try:
        repo, cfg = _find_repo(), _load_cfg(args)
        if args.run:
            view = metrics_view(repo, cfg, run_id=find_run(repo, args.run).id)
        else:
            view = metrics_view(repo, cfg, args.lane,
                                date.fromisoformat(args.date_from) if args.date_from else None,
                                date.fromisoformat(args.date_to) if args.date_to else None, all_time=args.all)
    except _human_errors() as exc:
        print(f"error: {exc}")
        return 1
    if args.json:
        print(_json.dumps(view, indent=2))
    elif args.csv:
        print(metrics_csv(view), end="")
    else:
        print(metrics_table(view))
    return 0


def cmd_scorecard(args: argparse.Namespace) -> int:
    """The finished runs' signals against a frozen baseline; `--freeze` writes the baseline."""
    import json as _json
    from datetime import UTC, date, datetime

    from agentflow.config import check_lane
    from agentflow.scorecard import freeze, scorecard_table, scorecard_view

    try:
        repo, cfg = _find_repo(), _load_cfg(args)
        if args.lane is not None:
            check_lane(cfg, args.lane)
        date_from = date.fromisoformat(args.date_from) if args.date_from else None
        date_to = date.fromisoformat(args.date_to) if args.date_to else None
        now = datetime.now(UTC)
        if args.replace and not args.freeze:
            raise ValueError("--replace goes with --freeze")
        if args.freeze:
            data = freeze(repo, args.lane, date_from, date_to, args.replace, now)
            print(f"froze the baseline of {', '.join(data['lanes']) or 'no lanes'}")
            return 0
        view = scorecard_view(repo, args.lane, date_from, date_to, now)
    except FileExistsError as exc:
        print(f"error: {exc}")
        return 1
    except (*_human_errors(), OSError) as exc:
        print(f"error: {exc}")
        return 1
    print(_json.dumps(view, indent=2) if args.json else scorecard_table(view))
    return 0


def cmd_lessons(args: argparse.Namespace) -> int:
    """Every lesson, with its target signal before and after the runs that carried it."""
    import json as _json

    from agentflow.views import lessons_table, lessons_view

    try:
        repo, cfg = _find_repo(), _load_cfg(args)
        view = lessons_view(repo, cfg)
    except _human_errors() as exc:
        print(f"error: {exc}")
        return 1
    print(_json.dumps(view, indent=2) if args.json else lessons_table(view))
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    from agentflow.run_control import pause
    from agentflow.state import find_run

    try:
        run = find_run(_find_repo(), args.run)
        pause(run, _by(args))
    except _human_errors() as exc:
        print(f"error: {exc}")
        return 1
    print(f"{run.id} paused: a live agent finishes its step, then the run stops. `agentflow resume {run.id}` continues it.")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    from agentflow.run_control import cancel
    from agentflow.state import find_run

    try:
        run = find_run(_find_repo(), args.run)
        killed = cancel(run, _by(args), cfg=_load_cfg(args) if args.config else None)
    except _human_errors() as exc:
        print(f"error: {exc}")
        return 1
    stopped = f"; stopped agent PID(s) {', '.join(map(str, killed))}" if killed else ""
    print(f"{run.id} cancelled{stopped}.")
    skipped = _cancel_skipped(run)
    for item in skipped:
        print(f"not signalled: PID {item.get('pid')} ({item.get('reason')})")
    if any("has no configured command" in str(item.get("reason")) for item in skipped):
        print("warning: no config could be loaded to match agent processes, so live agents were not signalled; "
              "stop them yourself or pass --config <path>.")
    return 0


def _cancel_skipped(run) -> list[dict]:
    """The `{pid, reason}` rows of the run's `run_cancelled` event: agent PIDs `cancel` would not signal."""
    run.refresh()
    for event in reversed(list(run.events.read())):
        if event.get("type") == "run_cancelled":
            return [i for i in event.get("skipped") or [] if isinstance(i, dict)]
    return []


def cmd_archive(args: argparse.Namespace) -> int:
    from agentflow.run_control import archive
    from agentflow.state import find_run

    try:
        run = find_run(_find_repo(), args.run)
        archive(run, _by(args))
    except _human_errors() as exc:
        print(f"error: {exc}")
        return 1
    print(f"{run.id} archived; its card moves to Done.")
    return 0


def cmd_retry(args: argparse.Namespace) -> int:
    from agentflow.run_control import retry
    from agentflow.state import find_run

    try:
        run = find_run(_find_repo(), args.run)
        key = retry(run, args.key, _by(args), reason=args.reason)
        print(f"retrying {key} for {run.id}.")
        return _continue(run, args)
    except _human_errors() as exc:
        print(f"error: {exc}")
        return 1


def cmd_attach(args: argparse.Namespace) -> int:
    from agentflow.run_control import attach
    from agentflow.state import find_run

    try:
        run = find_run(_find_repo(), args.run)
        code = attach(run, _run_cfg(run, args))
    except _human_errors() as exc:
        print(f"error: {exc}")
        return 1
    print(f"Session closed (exit {code}). Continue the run with `agentflow resume {run.id}`.")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    import secrets

    from agentflow.dashboard import serve

    try:
        cfg = _load_cfg(args)
        repo = _find_repo()
        port = args.port if args.port is not None else int(cfg.dashboard.get("port", 7788))
        config = Path(args.config).resolve() if args.config else None
        serve(repo, cfg, str(cfg.dashboard.get("host", "127.0.0.1")), port, secrets.token_urlsafe(32), config=config)
    except (_human_errors() + (OSError,)) as exc:
        print(f"error: {exc}")
        return 1
    return 0


def _continue_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--no-continue", action="store_true",
                        help="only record (an approved phase rests as ready); continue later with `agentflow resume`")
    parser.add_argument("--detach", action="store_true", help="continue the run in the background")
    parser.add_argument("--config", help="path to a config.yaml (default: the one the run started with)")


def _since_date(text: str):
    """`--since YYYY-MM-DD` as midnight UTC of that day."""
    from datetime import UTC, datetime

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise argparse.ArgumentTypeError(f"{text!r} is not a date: use YYYY-MM-DD")
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a date: {exc}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentflow", description="Run the repo's agent workflow, from idea to ship.")
    sub = parser.add_subparsers(dest="command", required=True)
    ini = sub.add_parser("init", help="write a sparse pack for this repository from what it holds (base branch, "
                                      "starter, gates, docs); runs no gate unless you pass --baseline")
    ini.add_argument("--starter", help="the starter to build on (default: picked from the repository's manifests)")
    ini.add_argument("--baseline", action="store_true",
                     help="run the detected gates once on the base branch, in a throwaway worktree, and mark a gate "
                          "that fails there `baseline: red`")
    ini.set_defaults(func=cmd_init)

    doc = sub.add_parser("doctor", help="check this machine can run the pack: agent CLIs and logins, gh, the remote, "
                                        "Docker, the state dir and each gate's programs")
    doc.add_argument("--config", help="path to a config.yaml (default: the repository's .agentflow/config.yaml)")
    doc.set_defaults(func=cmd_doctor)

    check = sub.add_parser("validate", help="check config.yaml: skills, prompts, conditions, references")
    check.add_argument("--config", help="path to a config.yaml (default: the repository's .agentflow/config.yaml)")
    check.set_defaults(func=cmd_validate)

    conf = sub.add_parser("config", help="show the pack file, or the effective config with each value's layer")
    conf_sub = conf.add_subparsers(dest="config_command", required=True)
    show = conf_sub.add_parser("show", help="print the pack file as written; --effective: the merged layers")
    show.add_argument("--effective", nargs="?", const=True, default=False, metavar="PREFIX",
                      help="every value after the engine defaults, the pack and your user file are merged, as "
                           "`<key>: <JSON>  [<layer>]`; a PREFIX (e.g. `defaults` or `agents.claude`) shows only its keys")
    show.add_argument("--config", help="path to a config.yaml (default: the repository's .agentflow/config.yaml)")
    show.set_defaults(func=cmd_config_show)

    status = sub.add_parser("status", help="list runs with phase, step, status and spend, or show one run")
    status.add_argument("run", nargs="?", help="a run id (or a part of it that matches one run)")
    status.add_argument("--watch", action="store_true", help="a live board in the terminal (Ctrl-C ends it)")
    status.add_argument("--config", help="path to a config.yaml (default: the repository's .agentflow/config.yaml)")
    status.set_defaults(func=cmd_status)

    agents = sub.add_parser("agents", help="every agent process running now, across runs")
    agents.add_argument("--watch", action="store_true", help="refresh on every new event (Ctrl-C ends it)")
    agents.add_argument("--config", help="path to a config.yaml (default: the repository's .agentflow/config.yaml)")
    agents.set_defaults(func=cmd_agents)

    inbox = sub.add_parser("inbox", help="every run waiting on a human, with its questions")
    inbox.set_defaults(func=cmd_inbox)

    ans = sub.add_parser("answer", help="answer the questions a run is waiting on")
    ans.add_argument("run")
    ans.add_argument("--set", action="append", default=[], metavar="Q=ANSWER", help="answer one question; repeat for more")
    ans.add_argument("--by", help="who is answering (default: the current user)")
    ans.add_argument("--key", help="answer a parked step's questions (default: what the run waits on)")
    ans.add_argument("--record", action="store_true",
                     help="write the answers into the spec as a decision (a spec_gap answer always is)")
    _continue_flags(ans)
    ans.set_defaults(func=cmd_answer)

    app = sub.add_parser("approve", help="approve a phase (plan or plan_approved) or clear an inner checkpoint")
    app.add_argument("run")
    app.add_argument("target")
    app.add_argument("--by", help="who is approving (default: the current user)")
    app.add_argument("--note", help="what to tell the agent an approved stop resumes (what changed, what to do)")
    app.add_argument("--budget", type=float, help="set the run's hard budget (USD); without it, approving a hard-budget "
                                                  "stop allows the spend so far plus one soft budget (run_budget_usd)")
    _continue_flags(app)
    app.set_defaults(func=cmd_approve)

    rej = sub.add_parser("reject", help="send a phase back for a refine round with a note")
    rej.add_argument("run")
    rej.add_argument("target")
    rej.add_argument("--note", required=True, help="what to change")
    rej.add_argument("--by", help="who is rejecting (default: the current user)")
    _continue_flags(rej)
    rej.set_defaults(func=cmd_reject)

    run = sub.add_parser("run", help="start the workflow for a lane; it stops for a human after each phase with needs_approval: true")
    run.add_argument("lane", help="the kind of work, which selects the workflow's steps: feature, bug, eval or design")
    for flag in PARAM_FLAGS:
        run.add_argument(f"--{flag}", help=f"the run's {flag}")
    run.add_argument("--from", dest="from_phase", help="start at this phase instead of the first")
    run.add_argument("--to", dest="to_phase", help="end the run once this phase is approved")
    run.add_argument("--model", action="append", default=[], metavar="PHASE=MODEL", help="override one phase's model")
    run.add_argument("--new-module", help="a module not in the capability map yet, as JSON "
                     '{"module": ..., "responsibility": ..., "deps": [...]}: the run adds its row first')
    run.add_argument("--dry-run", action="store_true", help="print the agent and gate commands; create and call nothing")
    run.add_argument("--detach", action="store_true", help="run in the background after starting")
    run.add_argument("--config", help="path to a config.yaml (default: the repository's .agentflow/config.yaml)")
    run.set_defaults(func=cmd_run)

    res = sub.add_parser("resume", help="continue a run from where it stopped")
    res.add_argument("run")
    res.add_argument("--mark-done", action="store_true",
                     help="on a ready run: skip the remaining phases and go straight to the last one, which opens the draft PR")
    res.add_argument("--by", help="who is marking the run done (default: the current user)")
    res.add_argument("--detach", action="store_true", help="continue in the background")
    res.add_argument("--config", help="path to a config.yaml (default: the one the run started with)")
    res.set_defaults(func=cmd_resume)

    exp = sub.add_parser("explain", help="show what a lane would run now, from files and git (no agent calls)")
    exp.add_argument("lane")
    exp.add_argument("--module", help="a capability-map module id")
    exp.add_argument("--topic", help="a design topic")
    exp.add_argument("--issue", help="a bug or issue reference")
    exp.add_argument("--model", action="append", default=[], metavar="PHASE=MODEL", help="override one phase's model")
    exp.add_argument("--config", help="path to a config.yaml (default: the repository's .agentflow/config.yaml)")
    exp.set_defaults(func=cmd_explain)

    nxt = sub.add_parser("next", help="the lane, stage and next skill for the repo as it stands now")
    nxt.add_argument("--config", help="path to a config.yaml (default: the repository's .agentflow/config.yaml)")
    nxt.set_defaults(func=cmd_next)

    blg = sub.add_parser("backlog", help="mark a Backlog module finished (or not) by hand")
    blg_sub = blg.add_subparsers(dest="backlog_command", required=True)
    for verb, text in (("done", "mark a module done by hand (refused while it has a live run)"),
                       ("undone", "remove a module's hand mark, or with none, ignore the origin evidence that says "
                                  "it is done")):
        one = blg_sub.add_parser(verb, help=text)
        one.add_argument("module", help="a module id from the work source")
        one.add_argument("--by", help="who is marking it (default: the current user)")
        one.add_argument("--config", help="path to a config.yaml (default: the repository's .agentflow/config.yaml)")
        one.set_defaults(func=cmd_backlog)

    que = sub.add_parser("queue", help="start modules in capability-map order as their dependencies merge")
    que.add_argument("--max-parallel", type=int, default=2, help="runs live at once (default 2)")
    que.add_argument("--once", action="store_true", help="tick one time and exit (for cron and tests)")
    que.add_argument("--interval", type=float, default=30.0, help="seconds between ticks (default 30)")
    que.add_argument("--include-specs", action="store_true",
                     help="with work_source specs, queue every spec file's module (refused without it)")
    que.add_argument("--config", help="path to a config.yaml (default: the repository's .agentflow/config.yaml)")
    que.set_defaults(func=cmd_queue)

    met = sub.add_parser("metrics", help="where runs spent time and money: by phase, wait, model and effort")
    met.add_argument("run", nargs="?", help="one run (default: every run started in the date range)")
    met.add_argument("--lane", help="only this lane's runs")
    met.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="runs started on or after (default: the 1st of this month)")
    met.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="runs started on or before (default: today)")
    met.add_argument("--all", action="store_true", help="every run, whenever it started")
    shape = met.add_mutually_exclusive_group()
    shape.add_argument("--json", action="store_true", help="the full data as JSON")
    shape.add_argument("--csv", action="store_true", help="one row per run, phase, model and effort")
    met.add_argument("--config", help="path to a config.yaml (default: the repository's .agentflow/config.yaml)")
    met.set_defaults(func=cmd_metrics)

    sco = sub.add_parser("scorecard", help="the finished runs' signals against a frozen baseline, conformance first")
    sco.add_argument("--lane", help="only this lane")
    sco.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="runs that ended on or after")
    sco.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="runs that ended on or before")
    sco.add_argument("--json", action="store_true", help="the full data as JSON")
    sco.add_argument("--freeze", action="store_true", help="record the baseline from the finished runs in range")
    sco.add_argument("--replace", action="store_true", help="with --freeze: replace a baseline that already exists")
    sco.add_argument("--config", help="path to a config.yaml (default: the repository's .agentflow/config.yaml)")
    sco.set_defaults(func=cmd_scorecard)

    les = sub.add_parser("lessons", help="each lesson's target signal before and after the runs that carried it")
    les.add_argument("--json", action="store_true", help="the full data as JSON")
    les.add_argument("--config", help="path to a config.yaml (default: the repository's .agentflow/config.yaml)")
    les.set_defaults(func=cmd_lessons)

    ret_o = sub.add_parser("retro", help="learn from the runs that ended since the last retro: a draft PR that "
                                         "changes only the lessons file")
    ret_o.add_argument("--since", type=_since_date, metavar="YYYY-MM-DD",
                       help="read the runs that ended after this day (default: since the last retro that finished)")
    ret_o.add_argument("--dry-run", action="store_true", help="print the digest and the prompt; create nothing")
    ret_o.add_argument("--config", help="path to a config.yaml (default: the repository's .agentflow/config.yaml)")
    ret_o.set_defaults(func=cmd_retro)

    pau = sub.add_parser("pause", help="stop a run before its next step (a live agent finishes its step)")
    pau.add_argument("run")
    pau.add_argument("--by", help="who is pausing (default: the current user)")
    pau.set_defaults(func=cmd_pause)

    can = sub.add_parser("cancel", help="end a run and stop its live agents")
    can.add_argument("run")
    can.add_argument("--by", help="who is cancelling (default: the current user)")
    can.add_argument("--config", help="path to a config.yaml (default: the one the run started with)")
    can.set_defaults(func=cmd_cancel)

    arc = sub.add_parser("archive", help="file a finished run away as old data: its card moves to Done")
    arc.add_argument("run")
    arc.add_argument("--by", help="who is archiving (default: the current user)")
    arc.set_defaults(func=cmd_archive)

    ret = sub.add_parser("retry", help="run a step again in a fresh session (default: the failed step), then go on")
    ret.add_argument("run")
    ret.add_argument("key", nargs="?", help="the step key to retry (default: the run's failed step)")
    ret.add_argument("--by", help="who is retrying (default: the current user)")
    ret.add_argument("--reason", metavar="TEXT", help="why the step is retried; recorded for the run's lessons")
    _continue_flags(ret)
    ret.set_defaults(func=cmd_retry)

    att = sub.add_parser("attach", help="open the waiting step's agent session interactively in its worktree")
    att.add_argument("run")
    att.add_argument("--config", help="path to a config.yaml (default: the one the run started with)")
    att.set_defaults(func=cmd_attach)

    das = sub.add_parser("dashboard", help="the local Kanban board and Agents view (127.0.0.1, token-protected)")
    das.add_argument("--port", type=int, help="the port (default: dashboard.port; 0 picks a free one)")
    das.add_argument("--config", help="path to a config.yaml (default: the repository's .agentflow/config.yaml)")
    das.set_defaults(func=cmd_dashboard)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # anything the commands do not turn into a message themselves
        traceback.print_exc(file=sys.stderr)
        print(f"error: the runner stopped on an unexpected {type(exc).__name__}: {exc}. A run it was working on "
              "is marked `error` (event `runner_error`): fix the cause, then `agentflow resume <run>`.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
