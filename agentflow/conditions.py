"""Named conditions, prompt placeholders and `agentflow explain`.

A condition is evaluated by the runner from files and git, never by a model. It comes out
`True`, `False` or `None` (unknown, with an `on_unknown` policy). A condition is a name from
`config.yaml`'s `conditions:` block, or an inline `{check: ...}` / `{all|any|not: ...}`.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentflow import pack, sources, work_source
from agentflow.config import ON_UNKNOWN_VALUES, Config, Phase, Step, check_lane, for_lane, resolve_model
from agentflow.gitops import GhError, changed_files, gh_json
from agentflow.guards import path_matches
from agentflow.state import SLUG_PARAMS, slugify

if TYPE_CHECKING:
    from agentflow.state import Run

COMMAND_TIMEOUT_S = 60
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_LABEL_ONLY = re.compile(r"^\*\*[^*]+\*\*:?$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
_DONE_STEP_STATUSES = ("done", "unchanged")


@dataclass
class Ctx:
    repo: Path
    cfg: Config
    values: dict[str, str]
    base_ref: str = "origin/main"
    run: Run | None = None


@dataclass
class CondResult:
    value: bool | None
    reason: str
    on_unknown: str | bool = "ask_human"


# --------------------------------------------------------------------------- context and fmt


def make_ctx(cfg: Config, repo: Path, pipeline: str, params: dict, run: Run | None = None) -> Ctx:
    """The placeholder values every prompt and condition can use, as strings."""
    repo = Path(repo)
    docs = cfg.project.docs
    module = str(params.get("module") or "")
    today = str(params.get("date") or date.today().isoformat())
    slug_source = ""
    for name in SLUG_PARAMS:
        value = params.get(name)
        if value not in (None, ""):
            slug_source = Path(str(value)).stem if name == "log" else str(value)
            break
    slug = slugify(slug_source) or slugify(pipeline)
    plan = sources.find_plan(repo / docs["plans"], module)
    values = {
        "module": module,
        "module_pkg": module.replace("-", "_"),
        "lane": pipeline,
        "spec": f"{docs['specs']}/{cfg.project.spec_file.format(module=module)}" if module else "",
        "plan": plan.relative_to(repo).as_posix() if plan else "",
        "plan_target": f"{docs['plans']}/{today}-{module}-{slug}.md" if module else "",
        "idea": docs.get("idea") or "",
        "capability_map": (cfg.work_source.path if cfg.work_source is not None
                           and cfg.work_source.kind == "capability_map" else ""),
        "topic": str(params.get("topic") or ""),
        "issue": str(params.get("issue") or ""),
        "run": run.id if run else "",
        "date": today,
        "slug": slug,
    }
    return Ctx(repo=repo, cfg=cfg, values=values, run=run)


def fmt(template: str, ctx_or_values: Ctx | dict[str, Any]) -> str:
    """Replace `{name}` for known names only. Everything else stays verbatim and nothing raises."""
    values = ctx_or_values.values if isinstance(ctx_or_values, Ctx) else ctx_or_values

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return str(values[name]) if name in values else match.group(0)

    return _PLACEHOLDER.sub(replace, template)


def skill_text(cfg: Config, repo: Path, name: object) -> str | None:
    """The body of skill `name`'s `SKILL.md` (the repository's, else the engine's bundled one), or None.

    `repo` is the main checkout: a run's worktree holds what an agent wrote, so it is never a source of skill text.
    A UTF-8 BOM and `\\r\\n` are normalised away; a frontmatter block (a first line `---` and a later line `---`)
    is dropped, and a file whose frontmatter never closes is all body."""
    file = pack.skill_file(repo, cfg.project.skills_dir, name)
    if file is None:
        return None
    try:
        text = file.read_text(encoding="utf-8").removeprefix("\ufeff").replace("\r\n", "\n")
    except (OSError, UnicodeDecodeError):
        return None
    lines = text.split("\n")
    if lines[0].rstrip() == "---":
        for number, line in enumerate(lines[1:], start=1):
            if line.rstrip() == "---":
                return "\n".join(lines[number + 1:]).lstrip("\n")
    return text


# --------------------------------------------------------------------------- helpers


def _word(value: bool | None) -> str:
    return "unknown" if value is None else str(value).lower()


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _path(spec: dict, key: str, ctx: Ctx) -> str:
    return fmt(str(spec.get(key) or ""), ctx).strip()


def _read_text(spec: dict, key: str, ctx: Ctx) -> tuple[str | None, str, str]:
    """`(text, relative_path, problem)`: `text` is None when the file is not there."""
    rel = _path(spec, key, ctx)
    if not rel:
        return None, rel, "no file"
    target = ctx.repo / rel
    if not target.is_file():
        return None, rel, f"{rel} is missing"
    return target.read_text(encoding="utf-8"), rel, ""


def _regex(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"bad pattern {pattern!r}: {exc}") from exc


# --------------------------------------------------------------------------- checks

Check = Callable[[dict, Ctx], tuple[bool | None, str]]


def _file_exists(spec: dict, ctx: Ctx) -> tuple[bool | None, str]:
    rel = _path(spec, "path", ctx)
    if not rel:
        return False, "no path"
    found = (ctx.repo / rel).exists()
    return found, f"{rel} {'exists' if found else 'is missing'}"


def _grep(spec: dict, ctx: Ctx) -> tuple[bool | None, str]:
    text, rel, problem = _read_text(spec, "file", ctx)
    if text is None:
        return False, problem
    raw = spec.get("pattern")
    if isinstance(raw, list):
        regex = re.compile("|".join(re.escape(str(k)) for k in raw) or "(?!)", re.IGNORECASE)
    else:
        regex = _regex(str(raw))
    for number, line in enumerate(text.splitlines(), start=1):
        if regex.search(line):
            return True, f"pattern found in {rel} at line {number}"
    return False, f"pattern not found in {rel}"


def _section_items(spec: dict, ctx: Ctx) -> tuple[bool | None, str]:
    text, rel, problem = _read_text(spec, "file", ctx)
    if text is None:
        return False, problem
    section = _regex(str(spec.get("section", "")))
    stops = [_regex(p) for p in _list(spec.get("stop_at"))]
    nones = [_regex(p) for p in _list(spec.get("none_patterns"))]
    match_text = fmt(str(spec["match"]), ctx).strip() if spec.get("match") else ""
    wanted = re.compile(re.escape(match_text), re.IGNORECASE) if match_text else None
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if section.search(line)), None)
    if start is None:
        return None, f"no section matching {spec.get('section')} in {rel}"
    open_items = resolved = 0
    after_stop = False
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        stripped = line.strip()
        if not stripped:
            continue
        if not after_stop and any(p.search(stripped) for p in stops):
            after_stop = True
        if wanted and not wanted.search(stripped):
            continue
        if after_stop:
            resolved += bool(_LIST_ITEM.match(line))
        elif not _LABEL_ONLY.match(stripped) and not any(p.search(stripped) for p in nones):
            open_items += 1
    minimum = int(spec.get("min", 1))
    return open_items >= minimum, f"{open_items} open items, {resolved} resolved"


def _status_line(spec: dict, ctx: Ctx) -> tuple[bool | None, str]:
    rel = _path(spec, "file", ctx)
    if not rel or not (ctx.repo / rel).is_file():
        return False, f"{rel or 'no file'} is missing"
    status = sources.spec_status(ctx.repo / rel, str(spec["approved"]), str(spec["pending"]))
    if status == "approved":
        return True, f"{rel} status line says approved"
    if status == "pending":
        return False, f"{rel} has an amendment pending approval"
    return None, f"{rel} has no status line"


def _changed_paths(spec: dict, ctx: Ctx) -> tuple[bool | None, str]:
    files = changed_files(ctx.repo, ctx.base_ref)
    if files is None:
        return None, f"cannot diff against {ctx.base_ref}"
    globs = _list(spec.get("globs"))
    hits = [p for p in files if any(path_matches(p, g) for g in globs)]
    if hits:
        return True, f"{len(hits)} changed paths match, e.g. {hits[0]}"
    return False, f"none of {len(files)} changed paths match"


def _plan_file(spec: dict, ctx: Ctx) -> tuple[list[sources.PlanTask] | None, str]:
    rel = _path(spec, "file", ctx)
    if not rel or not (ctx.repo / rel).is_file():
        return None, "no plan file"
    return sources.parse_plan(ctx.repo / rel), rel


def _plan_tasks(spec: dict, ctx: Ctx) -> tuple[bool | None, str]:
    tasks, rel = _plan_file(spec, ctx)
    if tasks is None:
        return None, rel
    n = len(tasks)
    maximum, minimum = spec.get("max"), spec.get("min")
    ok = (maximum is None or n <= int(maximum)) and (minimum is None or n >= int(minimum))
    bounds = ", ".join(f"{k} {v}" for k, v in (("min", minimum), ("max", maximum)) if v is not None)
    return ok, f"{n} task{'' if n == 1 else 's'}" + (f" ({bounds})" if bounds else "")


def _plan_unchecked(spec: dict, ctx: Ctx) -> tuple[bool | None, str]:
    tasks, rel = _plan_file(spec, ctx)
    if tasks is None:
        return False, rel
    unchecked = [t for t in tasks if t.open]
    return bool(unchecked), f"{len(unchecked)} of {len(tasks)} tasks unchecked"


def _merged_heads(ctx: Ctx) -> set[str]:
    prs = gh_json(["pr", "list", "--state", "merged", "--json", "headRefName", "--limit", "200"], cwd=ctx.repo)
    return {str(pr.get("headRefName", "")) for pr in prs or []}


def _modules_done(spec: dict, ctx: Ctx) -> tuple[bool | None, str]:
    """Whether every module the run's module depends on (in the pack's work source) has a merged pull request."""
    module = ctx.values.get("module", "")
    nodes = work_source.items(ctx.cfg, ctx.repo)
    node = next((n for n in nodes if n.id == module), None)
    if node is None:
        return None, f"module '{module}' is not in the {work_source.noun(ctx.cfg)}"
    if not node.deps:
        return True, f"{module} has no dependencies"
    try:
        heads = _merged_heads(ctx)
    except GhError as exc:
        return None, f"gh failed: {exc}"
    template = ctx.cfg.project.branch.replace("{lane}", "feature")
    missing = [
        dep
        for dep in node.deps
        if not any(re.fullmatch(re.escape(template.replace("{slug}", dep)) + r"(-\d+)?", h) for h in heads)
    ]
    if missing:
        return False, f"not merged yet: {', '.join(missing)}"
    return True, f"merged: {', '.join(node.deps)}"


def _gate_passed(spec: dict, ctx: Ctx) -> tuple[bool | None, str]:
    name = str(spec.get("gate", ""))
    if ctx.run is None:
        return None, "no run to read gate results from"
    entries = [e for rows in ctx.run.state["gates"].values() for e in rows if e.get("gate") == name]
    if not entries:
        return None, f"gate {name} has not run"
    last = entries[-1]
    if last.get("skipped"):
        return None, f"gate {name} was skipped"
    return bool(last.get("passed")), f"gate {name} {'passed' if last.get('passed') else 'failed'}"


def _branch_merged(spec: dict, ctx: Ctx) -> tuple[bool | None, str]:
    branch = _path(spec, "branch", ctx) or ctx.values.get("branch", "")
    if not branch:
        return None, "no branch to look for"
    try:
        prs = gh_json(
            ["pr", "list", "--head", branch, "--state", "merged", "--json", "number,mergedAt", "--limit", "1"],
            cwd=ctx.repo,
        )
    except GhError as exc:
        return None, f"gh failed: {exc}"
    merged = bool(prs)
    return merged, f"{branch} {'is merged' if merged else 'has no merged pull request'}"


def _result_signal(spec: dict, ctx: Ctx) -> tuple[bool | None, str]:
    if ctx.run is None:
        return None, "no run to read a result from"
    key = str(spec.get("step") or "")
    if not key:
        done = [k for k, row in ctx.run.state["steps"].items() if row.get("status") in _DONE_STEP_STATUSES]
        if not done:
            return None, "no step has finished yet"
        key = done[-1]
    path = ctx.run.result_path(key)
    if not path.is_file():
        return None, f"{key} wrote no result.json"
    try:
        node: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None, f"{key}'s result.json is not valid JSON"
    dotted = str(spec.get("signal", ""))
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, f"{dotted} is absent from {key}'s result"
        node = node[part]
    if "equals" in spec:
        return node == spec["equals"], f"{dotted} is {node!r} in {key}'s result"
    return bool(node), f"{dotted} is {node!r} in {key}'s result"


def _command(spec: dict, ctx: Ctx) -> tuple[bool | None, str]:
    command = fmt(str(spec.get("run", "")), ctx)
    try:
        proc = subprocess.run(
            command, shell=True, cwd=ctx.repo, capture_output=True, stdin=subprocess.DEVNULL, timeout=COMMAND_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        return None, f"`{command}` timed out after {COMMAND_TIMEOUT_S} s"
    return proc.returncode == 0, f"`{command}` exit {proc.returncode}"


def _lane(spec: dict, ctx: Ctx) -> tuple[bool | None, str]:
    lane, wanted = ctx.values.get("lane"), spec.get("is")
    return lane == wanted, f"this run's lane is {lane}, not {wanted}" if lane != wanted else f"this run's lane is {lane}"


def _refine_round(spec: dict, ctx: Ctx) -> tuple[bool | None, str]:
    refine = (ctx.run.state.get("refine") if ctx.run is not None else None) or {}
    if refine:
        return True, f"refine round {refine.get('round')} of {refine.get('phase')}"
    return False, "not in a refine round"


CHECKS: dict[str, Check] = {
    "file_exists": _file_exists,
    "grep": _grep,
    "section_items": _section_items,
    "status_line": _status_line,
    "changed_paths": _changed_paths,
    "plan_tasks": _plan_tasks,
    "plan_unchecked": _plan_unchecked,
    "modules_done": _modules_done,
    "gate_passed": _gate_passed,
    "branch_merged": _branch_merged,
    "result_signal": _result_signal,
    "command": _command,
    "lane": _lane,
    "refine_round": _refine_round,
}


# --------------------------------------------------------------------------- evaluate


def _apply_on_unknown(result: CondResult, policy: Any) -> CondResult:
    if policy not in ON_UNKNOWN_VALUES:
        raise ValueError(f"on_unknown must be one of {list(ON_UNKNOWN_VALUES)}, got {policy!r}")
    if result.value is None and isinstance(policy, bool):
        return CondResult(policy, f"{result.reason} (unknown, treated as {_word(policy)})", policy)
    if result.value is None:
        return CondResult(None, result.reason, policy)
    return result


def _first_unknown(results: list[CondResult]) -> str | bool:
    return next((r.on_unknown for r in results if r.value is None), "ask_human")


def evaluate(cfg: Config, cond: str | dict, ctx: Ctx, _stack: tuple[str, ...] = ()) -> CondResult:
    """Evaluate a named condition or an inline check/composition to true, false or unknown."""
    if isinstance(cond, str):
        if cond not in cfg.conditions:
            raise ValueError(f"undefined condition '{cond}'")
        if cond in _stack:
            raise ValueError(f"condition cycle: {' -> '.join((*_stack, cond))}")
        inner = evaluate(cfg, cfg.conditions[cond], ctx, (*_stack, cond))
        return CondResult(inner.value, f"{cond} = {_word(inner.value)}: {inner.reason}", inner.on_unknown)
    if not isinstance(cond, dict):
        raise ValueError(f"a condition is a name or a mapping, got {cond!r}")
    if "check" in cond:
        name = str(cond["check"])
        if name not in CHECKS:
            raise ValueError(f"unknown check '{name}'")
        value, reason = CHECKS[name](cond, ctx)
        result = CondResult(value, reason)
    elif "not" in cond:
        inner = evaluate(cfg, cond["not"], ctx, _stack)
        value = None if inner.value is None else not inner.value
        result = CondResult(value, f"not ({inner.reason})", inner.on_unknown)
    elif "any" in cond or "all" in cond:
        kind = "any" if "any" in cond else "all"
        decider = kind == "any"  # `any` stops at True, `all` stops at False
        results: list[CondResult] = []
        for member in cond[kind]:
            member_result = evaluate(cfg, member, ctx, _stack)
            results.append(member_result)
            if member_result.value is decider:
                break
        reasons = "; ".join(r.reason for r in results)
        if results[-1].value is decider:
            result = CondResult(decider, f"{kind}: {reasons}")
        elif any(r.value is None for r in results):
            result = CondResult(None, f"{kind}: {reasons}", _first_unknown(results))
        else:
            result = CondResult(not decider, f"{kind}: {reasons}")
    else:
        raise ValueError("a condition needs one of check, all, any or not")
    if "on_unknown" in cond:
        result = _apply_on_unknown(result, cond["on_unknown"])
    return result


def paths_read(cfg: Config, cond: str | dict | None, ctx: Ctx, _stack: tuple[str, ...] = ()) -> set[str]:
    """The files a condition's checks read (their `path`/`file`), with placeholders filled in."""
    if isinstance(cond, str):
        if cond not in cfg.conditions or cond in _stack:
            return set()
        return paths_read(cfg, cfg.conditions[cond], ctx, (*_stack, cond))
    if not isinstance(cond, dict):
        return set()
    if "check" in cond:
        return {path for key in ("path", "file") if (path := _path(cond, key, ctx))}
    if "not" in cond:
        return paths_read(cfg, cond["not"], ctx, _stack)
    found: set[str] = set()
    for member in cond.get("all") or cond.get("any") or []:
        found |= paths_read(cfg, member, ctx, _stack)
    return found


# --------------------------------------------------------------------------- explain


@dataclass
class ExplainLine:
    phase: str
    key: str
    verdict: str  # run | skip | ask | approval
    reason: str
    model: str | None = None


def _step_verdict(cfg: Config, step: Step, ctx: Ctx) -> tuple[str, str]:
    """`(verdict, reason)` from the step's `when` and `unless`."""
    reasons: list[str] = []
    if step.when is not None:
        result = evaluate(cfg, step.when, ctx)
        if result.value is False:
            return "skip", result.reason
        if result.value is None:
            return "ask", result.reason
        reasons.append(result.reason)
    if step.unless is not None:
        result = evaluate(cfg, step.unless, ctx)
        if result.value is True:
            return "skip", result.reason
        if result.value is None:
            return "ask", result.reason
        reasons.append(result.reason)
    return "run", "; ".join(reasons)


def _model_for(cfg: Config, phase: Phase, step: Step, role: str | None, overrides: dict[str, str]) -> str:
    return resolve_model(cfg, phase, step, role or "build", overrides)[0]


def _explain_steps(
    cfg: Config, phase: Phase, steps: list[Step], prefix: str, ctx: Ctx, overrides: dict[str, str]
) -> list[ExplainLine]:
    lines: list[ExplainLine] = []
    for step in for_lane(steps, ctx.values["lane"]):
        key = f"{prefix}/{step.id}"
        verdict, reason = _step_verdict(cfg, step, ctx)
        model = _model_for(cfg, phase, step, step.role, overrides) if step.type == "agent" else None
        lines.append(ExplainLine(phase.name, key, verdict, reason, model))
        if verdict == "skip":
            continue
        if step.review is not None:
            review_model = _model_for(cfg, phase, step.review, step.review.role or "review", overrides)
            lines.append(ExplainLine(phase.name, f"{key}/review", verdict, "per-task review", review_model))
        if step.steps:
            lines.extend(_explain_steps(cfg, phase, step.steps, key, ctx, overrides))
    return lines


def explain(cfg: Config, lane: str, ctx: Ctx, overrides: dict[str, str] | None = None) -> list[ExplainLine]:
    """What a run of `lane` would do right now, from files and git. Never calls an agent.

    Every phase gets a line under its own name (`run`, `skip` or `ask`); a phase that is not
    skipped lists the steps of this lane and, unless it is the workflow's last phase, the
    `<phase>_approved` stop.
    """
    check_lane(cfg, lane)
    overrides = overrides or {}
    names = list(cfg.phases)
    lines: list[ExplainLine] = []
    for index, name in enumerate(names):
        phase = cfg.phases[name]
        verdict, reason = "run", ""
        if phase.skip_if is not None:
            result = evaluate(cfg, phase.skip_if, ctx)
            verdict = "skip" if result.value is True else "ask" if result.value is None else "run"
            reason = result.reason
        if not for_lane(phase.steps, lane):
            verdict, reason = "skip", f"no steps for lane {lane}"
        lines.append(ExplainLine(name, name, verdict, reason, None))
        if verdict == "skip":
            continue
        step_lines = _explain_steps(cfg, phase, phase.steps, name, ctx, overrides)
        lines.extend(step_lines)
        ran = any(line.verdict in ("run", "ask") for line in step_lines)
        if ran and phase.needs_approval:
            why = "a human approves the finished workflow" if index == len(names) - 1 else "a human approves this phase before the next one"
            lines.append(ExplainLine(name, f"{name}_approved", "approval", why, None))
    return lines


def first_phase_with_work(cfg: Config, lane: str, ctx: Ctx) -> str | None:
    """The first phase a run of `lane` would do anything in right now, or None when it would skip them all.

    It uses the verdicts `explain` gives and the runner follows, so a phase with nothing left, such as `define`
    for a finished spec, is passed over just as a run would pass it. It stops at its answer, so the conditions
    of later phases, which may call git, are not evaluated."""
    for name, phase in cfg.phases.items():
        skip = evaluate(cfg, phase.skip_if, ctx).value if phase.skip_if is not None else False
        if skip is True:
            continue
        lines = _explain_steps(cfg, phase, phase.steps, name, ctx, {})
        if skip is None or any(line.verdict != "skip" for line in lines):
            return name
    return None


def plan_warnings(ctx: Ctx) -> list[str]:
    """Plan tasks that look too big for one session."""
    rel = ctx.values.get("plan", "")
    if not rel:
        return []
    warnings: list[str] = []
    for task in sources.parse_plan(ctx.repo / rel):
        warnings.extend(sources.task_size_warnings(task))
    return warnings
