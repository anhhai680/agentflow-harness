"""Load and validate `config.yaml`: typed dataclasses, `@a.b.c` references, clear errors.

`load_config` fails fast with a `ConfigError` (carrying the dotted YAML path) for anything
that cannot be turned into the dataclasses. `validate` then reports every semantic problem
it can find as a list of `Issue`s, so one run shows them all.
"""

from __future__ import annotations

import copy
import math
import posixpath
import re
import shlex
import subprocess
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from agentflow import layers, pack

KNOWN_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
SIZER_STEP_ID = re.compile(r"size(?:-\d+)?")  # the last part of a sizer's key (`sizing.is_sizer_key`): no step's id
RETRO_PIPELINE = "retro"  # the retro run's pipeline name: its runs never count as evidence for or against a lesson
BUILTIN_CHECKS = (
    "file_exists",
    "grep",
    "section_items",
    "status_line",
    "changed_paths",
    "plan_tasks",
    "plan_unchecked",
    "modules_done",
    "gate_passed",
    "branch_merged",
    "result_signal",
    "command",
    "lane",
    "refine_round",
)
BASELINES = ("red", "green")  # what a gate's `baseline` may say about it on the base branch when the pack was set up
ON_UNKNOWN_VALUES = ("ask_human", "agent", True, False)
BACKLOG_KEYS = ("sync_base", "module_lane", "new_task", "module_row", "lane_labels")
BACKLOG_FIELDS = ("issue", "task", "topic", "new_module", "after")  # the form fields agentflow knows
STEP_TYPES = ("agent", "gate", "group", "checkpoint", "finish", "commit", "ci")
EXPLICIT_STEP_TYPES = ("checkpoint", "finish", "commit", "ci")
PHASE_KEYS = ("agent", "model", "skip_if", "steps", "needs_approval", "resolve", "approval_note")
APPROVAL_PHASE_KEYS = ("approval", "auto_approve")
PHASE_APPROVAL_KEYS = ("refine_prompt", "show")
REMOVED_DEFAULTS = {  # keys that were read but never acted on: a value here would look like it does something
    "commit": "a step's own `commit:` decides when the runner commits",
    "push": "`finish` always pushes the run's own branch",
    "pr": "`finish` always opens a draft pull request, and the `ci` step marks it ready",
}
AUTONOMY_KEYS = ("non_blocking_severities", "finding_rounds", "resolve_kinds", "answer_recommended", "revert_forbidden",
                 "human_question_kinds")
# The `kind` of a `needs_human` question. `boundary`: the repository's docs reserve the decision for a human.
QUESTION_KINDS = ("spec_gap", "ambiguity", "conflict", "review_dispute", "boundary", "other")
CI_KEYS = ("poll_s", "appear_min", "timeout_min", "fix_rounds", "rerun_rounds", "sync_rounds")
FLOOR_BLOCKING = ("critical", "high", "medium")  # severities that always block, whatever `non_blocking_severities` lists
STEP_KEYS = (
    "id",
    "type",
    "skill",
    "role",
    "prompt",
    "output",
    "when",
    "unless",
    "repeat",
    "next",
    "gates",
    "fix_loop",
    "gates_first",
    "review",
    "commit",
    "message",
    "foreach",
    "expect",
    "model",
    "wait_for",
    "checks",
    "on_fail",
    "steps",
    "lanes",
    "tdd",
    "small_plan",
    "batch",
    "brief",
    "task_briefs",
    "name",
    "blocks_on_findings",
    "reconciles",
    "forbid",
    "fix",
    "sync",
    "human_paths",
)


# The design lane's built-in doc checks a step may name in `checks:` (`gates.run_doc_checks`).
DOC_CHECK_NAMES = ("decisions_log_row", "open_question_removed", "adr_index_updated")


# Placeholders a prompt template may use, as `{name}`. The first group is Task 5's `make_ctx`
# keys; the second is the per-step extras the runner adds. Later tasks extend this set when
# they add a placeholder, so a typo in a template fails `validate` instead of reaching an agent.
PROMPT_PLACEHOLDERS = frozenset(
    {
        # make_ctx
        "module",
        "module_pkg",
        "lane",
        "spec",
        "plan",
        "plan_target",
        "idea",
        "capability_map",
        "topic",
        "issue",
        "run",
        "date",
        "slug",
        # per-step extras
        "task_n",
        "task_title",
        "task_text",
        "refine_note",
        "refine_round",
        "previous_summary",
        "refine_artifacts",
        "question",
        "answer",
        "evidence",
        "step_id",
        "progress",
        "handoff",
        "questions",
        "run_dir",
        "result_file",
        "effort_reason",
        "diff_range",
        "task_start_sha",
        "earlier_findings",
        "gate_evidence",
        "brief",
        "phase",
        "stop_kind",
        "stop_key",
        "stop_summary",
        "stop_report",
        "run_facts",
        "lessons",
        "code_map",
        "ci_report",
        "sync_report",
        "skill",
        "base_ref",
        "self_checks",
        "reconcile_output",
        # sizer prompts (`sizing_lane.py`)
        "task_card",
        "task_cards",
    }
)
PERMISSION_FLAGS = ("--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools",
                    "--dangerously-skip-permissions", "--permission-mode")
PERMISSION_MODES = ("default", "acceptEdits", "plan", "dontAsk", "auto", "bypassPermissions")
EXPECT_VALUES = ("red", "green")
ON_FAIL_VALUES = ("block",)
CONFORMANCE_FAILED = "conformance_failed"  # the audit at `finish` found an obligation with no evidence (`block` mode)
RECONCILE_CHANGED = "reconcile_changed_conditions"  # a reconcile edit changed which steps of its phase run
NEVER_RESOLVED = ("budget_exceeded", "phase_approval", "checkpoint", "condition", "max_rounds", "agent_unavailable",
                  CONFORMANCE_FAILED, RECONCILE_CHANGED)
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """A config problem too big to continue; `path` is the dotted YAML path (`steps[2]`)."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}" if path else message)
        self.path = path
        self.message = message


@dataclass
class Issue:
    level: Literal["error", "warning"]
    path: str
    message: str
    fixable: bool = False  # `agentflow validate --fix` repairs it


DEFAULT_SKILLS_DIR = ".claude/skills"
DEFAULT_SPEC_FILE = "{module}.md"  # a module's spec is `<docs.specs>/<spec_file>`
# A spec header line that says the module is finished, matched per line with `re.IGNORECASE`: `Status: done`,
# `**Status:** done`, `**Status: shipped** 2026-01-02`, `- **Status:** done`, `> Status: done`; never `done-ish`
DEFAULT_DONE_STATUS = r"^[-*>\s]*Status[\s:*]+(done|shipped|implemented)(?![\w-])"
REQUIRED_DOCS = ("specs", "plans")  # `project.docs` keys every pack sets; `idea`, `decisions`, `glossary` are optional
# `capability_map`: a Markdown table with `Module id` and `Depends on` columns (`sources.capability_map`);
# `specs`: one module per spec file in `project.docs.specs`, its dependencies from the header (`sources.spec_deps`)
WORK_SOURCE_KINDS = ("capability_map", "specs")
SKILLS_MAP_KEYS = ("decide", "define", "design_doc", "plan", "build", "build_small", "verify", "ship",
                   "review", "debug", "review_feedback", "doubt")  # review_feedback: the skill sent with blocking findings


@dataclass
class Project:
    base_branch: str
    fork_from: str
    branch: str
    worktrees_dir: str | None  # None: the state dir's `worktrees/` (`worktrees_root`)
    docs: dict[str, str]
    commit_trailer: str
    skills_dir: str = DEFAULT_SKILLS_DIR
    spec_file: str = DEFAULT_SPEC_FILE
    done_status: str = DEFAULT_DONE_STATUS


@dataclass
class WorkSource:
    """Where the modules `agentflow queue`, the Backlog and `modules_done` walk come from (`work_source:`)."""

    kind: Literal["capability_map", "specs"]
    path: str  # relative to the repository root; `""` for `specs`, which always reads `project.docs.specs`


@dataclass
class AgentSpec:
    name: str
    enabled: bool
    command: list[str]
    base_args: list[str]
    permission_mode: str | None
    models: dict[str, str]
    effort_arg: list[str]
    max_turns: int | None
    profile: str | None
    skill_loading: str
    prices: dict[str, dict[str, float]] = field(default_factory=dict)  # USD per 1M tokens, by model id


PRICE_KEYS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


@dataclass
class EnvInherit:
    """`defaults.env_inherit`: what a run's agents and gates do not inherit from the process that started it."""

    drop: list[str] = field(default_factory=list)  # names or fnmatch globs
    drop_dotenv: list[str] = field(default_factory=lambda: [".env"])  # repo-relative files: a same-valued variable is dropped


@dataclass
class Defaults:
    agent: str
    step_timeout_min: int
    fix_attempts: int
    run_budget_usd: float  # the soft limit: crossing it notifies once, and the run goes on
    gate_timeout_min: float = 30
    env: dict[str, str] = field(default_factory=dict)  # for every agent session and gate of a run
    task_budget_usd: float = 2  # a feature run's soft budget grows to fit its plan (`feature_lane._scale_budget`); 0 = off
    env_inherit: EnvInherit = field(default_factory=EnvInherit)  # the inherited variables a run does not pass on
    run_budget_hard_usd: float = 65  # the hard limit: the run stops for a human's approval when it crosses it


@dataclass
class Recovery:
    give_up_h: float = 24
    failed_attempts: int = 3


@dataclass
class EffortPolicy:
    default: str
    allowed: list[str]
    sizing: bool = True


@dataclass
class ContextPolicy:
    autocompact_pct: int
    autocompact_env: str
    rollover_pct: int
    max_rollovers: int
    max_compactions: int
    context_window_tokens: int | str
    default_window_tokens: int
    progress_file: str
    gate_output_lines: int


@dataclass
class MemoryPolicy:
    lessons_max_tokens: int  # the most tokens of lessons a prompt carries; 0 turns lessons off
    digest_tokens: int  # the size a retro's digest of recent runs is cut to
    retro_every_days: int  # days between retros the runner proposes; 0 means manual only


@dataclass
class IssuePolicy:
    in_progress_label: str = "agentflow:in-progress"  # "" turns off the label and the start comment, never the PR link
    comment: bool = True  # also post one comment when a run starts from an issue
    epic_labels: tuple[str, ...] = ("epic",)  # an issue with one of these gets `Refs #N`, never `Closes #N`
    sync_label: str = ""  # open issues with this label become Backlog cards; "" turns the sync off
    sync_lane: str = "feature"  # the lane of a synced issue that no `lane_labels` entry matches
    lane_labels: dict[str, str] = field(default_factory=lambda: {"bug": "bug"})  # issue label: lane, first match wins


FLOOR_ID = "engine_floor"  # the obligation code adds after the layers merge: no layer can remove or replace it
EVIDENCE_KINDS = ("gates", "step", "approval", "expect")
ENFORCE_MODES = ("report", "block")
EXPECT_EVIDENCE = ("red_then_green",)


@dataclass(frozen=True)
class Evidence:
    kind: str  # one of EVIDENCE_KINDS
    arg: str  # a gate set, a step id, a phase name, or `red_then_green`

    def __str__(self) -> str:
        return f"{self.kind}:{self.arg}"


def parse_evidence(text: str) -> Evidence | None:
    """`kind:arg` as an `Evidence`; None for an unknown kind, an empty arg, or an `expect:` other than `red_then_green`."""
    kind, sep, arg = text.partition(":")
    if not sep or kind not in EVIDENCE_KINDS or not arg.strip() or arg != arg.strip():
        return None
    if kind == "expect" and arg not in EXPECT_EVIDENCE:
        return None
    return Evidence(kind, arg)


UNRECOGNISED = Evidence("unrecognised", "")  # stands in for an item that does not parse: never satisfied


@dataclass(frozen=True)
class Obligation:
    id: str
    require: tuple[Evidence, ...]
    when: str | None = None  # a condition name; absent: the obligation always applies
    lanes: tuple[str, ...] = ()  # absent: every lane


@dataclass
class Policy:
    enforce: str = "report"  # report | block
    obligations: dict[str, Obligation] = field(default_factory=dict)  # the floor is always last
    disabled: tuple[str, ...] = ()  # engine default ids a layer switched off (and the floor, if nulled), for `validate`


@dataclass
class Gate:
    name: str
    run: str
    cwd: str | None = None
    when_exists: str | None = None
    baseline: str | None = None  # `red`: it failed on the base branch when the pack was set up (a human fixes or deletes it)
    agent_check: bool = False  # the build prompt lists it as a check the implementer runs itself (`{self_checks}`)


@dataclass
class Guardrails:
    forbidden_paths: list[str]
    reject_trailers: list[str]
    user_only_skills: list[str]


@dataclass
class Step:
    id: str
    type: str
    skill: str | None = None
    role: str | None = None
    prompt: str | None = None
    output: str | None = None
    when: Any = None
    unless: Any = None
    repeat: dict | None = None
    next: list[dict] | None = None
    gates: str | list[str] | None = None
    fix_loop: bool = False
    gates_first: bool = False  # run `gates` before the session and hand it their output (`{gate_evidence}`)
    review: Step | None = None
    commit: bool | str = False
    message: str | None = None
    foreach: str | None = None
    expect: str | None = None
    model: str | None = None
    wait_for: str | None = None
    checks: list[str] = field(default_factory=list)
    on_fail: str | None = None
    steps: list[Step] = field(default_factory=list)
    lanes: list[str] = field(default_factory=list)
    tdd: bool = True
    small_plan: dict | None = None
    batch: dict | None = None  # a `foreach: plan_task` step: neighbouring small tasks share a session and a review
    brief: list[str] = field(default_factory=list)
    task_briefs: dict[str, str] = field(default_factory=dict)
    task_brief: list[str] = field(default_factory=list)  # run time only, never configured: the `task_briefs` a plan task matched
    continuation: bool = False  # run time only: a later batch member or shared small_plan task; its session has the skill
    name: str | None = None
    blocks_on_findings: bool = False  # its `findings` hold the phase back (`autonomy.phase_findings`)
    reconciles: bool = False  # in a `needs_approval: true` phase: the step that revises the artifact after a review's findings
    forbid: list[str] = field(default_factory=list)  # guardrails.forbidden_paths for this step only
    fix: Step | None = None  # a `ci` step: the session that fixes a failed check
    sync: Step | None = None  # a `finish` step: the session that resolves the target branch's conflicts
    human_paths: list[str] = field(default_factory=list)  # a `sync` step: conflicts in these paths are a human's
    path: str = ""


@dataclass
class Phase:
    name: str
    agent: str | None
    model: str | dict[str, str] | None
    skip_if: Any
    steps: list[Step]
    needs_approval: bool = True  # a phase that does not say keeps its human
    resolve: bool = False  # stops in this phase go to the resolver first (`autonomy.Resolver`)
    approval_note: dict | None = None  # a line on this phase's approval page, when its condition holds


@dataclass
class Config:
    version: int
    project: Project
    agents: dict[str, AgentSpec]
    defaults: Defaults
    effort: EffortPolicy
    context: ContextPolicy
    memory: MemoryPolicy
    gates: dict[str, Gate]
    gate_sets: dict[str, list[str]]
    guardrails: Guardrails
    overlays: dict[str, dict]
    conditions: dict[str, dict]
    lanes: list[str]
    phases: dict[str, Phase]
    record_step: Step | None
    phase_approval: dict
    checkpoints: dict[str, str]
    hitl: dict
    dashboard: dict
    raw: dict
    path: Path
    recovery: Recovery = field(default_factory=Recovery)
    autonomy: dict = field(default_factory=dict)
    resolve_step: Step | None = None
    retro_step: Step | None = None  # the one session of a retro run (`retro.run_retro`); not part of any lane
    ci: dict = field(default_factory=dict)
    work_source: WorkSource | None = None  # None: no modules to queue; `next` and the Backlog go on without them
    skills_map: dict[str, str] = field(default_factory=dict)  # the skill `next` names per kind of step (`skills.map`)
    merged: dict = field(default_factory=dict)  # the layers merged, before `@` resolution (`raw` is after it)
    layer_origins: dict[tuple[str, ...], str] = field(default_factory=dict)  # each leaf of `merged`: its layer's name
    issues: IssuePolicy = field(default_factory=IssuePolicy)  # what a run started from an issue does on GitHub
    policy: Policy = field(default_factory=Policy)  # the obligations a finished run must show evidence for


def for_lane(steps: list[Step], lane: str) -> list[Step]:
    """The steps a run of `lane` walks: those with no `lanes:` tag, and those tagged with `lane`."""
    return [step for step in steps if not step.lanes or lane in step.lanes]


def check_lane(cfg: Config, lane: str) -> None:
    """Raise `ValueError` for a lane the workflow does not know."""
    if lane == "spec":
        raise ValueError("there is no spec lane: use `agentflow run feature --module <module> --to define`")
    if lane not in cfg.lanes:
        raise ValueError(f"unknown lane {lane!r}; known: {', '.join(cfg.lanes)}")


def worktrees_root(cfg: Config, repo: Path) -> Path:
    """The directory a run's worktree is made in: `project.worktrees_dir` under the repository when set, else the
    state dir's `worktrees/` (outside the repository, so nothing needs a `.gitignore` line)."""
    if cfg.project.worktrees_dir:
        return repo / cfg.project.worktrees_dir
    return pack.state_dir(repo) / "worktrees"


def layer_files(cfg: Config) -> list[Path]:
    """The files `cfg` was built from, bottom-up: the engine defaults, the pack's starter when it names one, the
    pack and the user's file when it exists. A pure function of `cfg` and the disk; `OSError` from looking for the
    user file is the caller's to catch."""
    files = [layers.ENGINE_DEFAULTS, Path(cfg.path).resolve()]
    try:
        starter = layers.starter_file(cfg.merged.get("starter"))
    except layers.LayerError:  # a pack loaded on its own (`layered=False`) never used the starter it names
        starter = None
    if starter is not None:
        files.insert(1, starter)
    try:
        user = layers.user_config_path()
    except RuntimeError:  # no home directory to look in: there is no user file
        return files
    if user.exists():
        files.append(user)
    return files


# --------------------------------------------------------------------------- references


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _lookup(root: dict, dotted: str, at: str) -> Any:
    node: Any = root
    for segment in dotted.split("."):
        if isinstance(node, dict) and segment in node:
            node = node[segment]
        elif isinstance(node, list) and segment.isdigit() and int(segment) < len(node):
            node = node[int(segment)]
        else:
            raise ConfigError(at, f"unresolved reference @{dotted}")
    return node


def _resolve(node: Any, path: str, root: dict, stack: tuple[str, ...], origins: dict[str, str]) -> Any:
    """Return `node` with every `"@a.b.c"` string replaced by a copy of what it names.

    `origins` maps the path of each reference to the path it finally points at, so steps
    pulled in through a reference keep the YAML path of where they are defined.
    """
    if isinstance(node, str) and node.startswith("@"):
        target = node[1:]
        if target in stack:
            chain = " -> ".join((*stack, target))
            raise ConfigError(path, f"reference cycle: {chain}")
        value = _lookup(root, target, path)
        resolved = _resolve(value, target, root, (*stack, target), origins)
        origins[path] = origins.get(target, target)
        return resolved
    if isinstance(node, dict):
        return {k: _resolve(v, _join(path, str(k)), root, stack, origins) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(v, f"{path}[{i}]", root, stack, origins) for i, v in enumerate(node)]
    return node


# --------------------------------------------------------------------------- builders


def _require(data: dict, key: str, path: str, kind: type | tuple[type, ...]) -> Any:
    here = _join(path, key)
    if key not in data:
        raise ConfigError(here, "missing")
    value = data[key]
    if not isinstance(value, kind):
        raise ConfigError(here, f"expected {_kind_name(kind)}, got {type(value).__name__}")
    return value


def _optional(data: dict, key: str, path: str, kind: type | tuple[type, ...], default: Any) -> Any:
    if key not in data or data[key] is None:
        return default
    return _require(data, key, path, kind)


def _kind_name(kind: type | tuple[type, ...]) -> str:
    kinds = kind if isinstance(kind, tuple) else (kind,)
    return " or ".join(k.__name__ for k in kinds)


def _string_map(value: Any, path: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ConfigError(path, "must map names to strings (quote numbers)")
    return dict(value)


def _env_inherit(value: Any) -> EnvInherit:
    path = "defaults.env_inherit"
    if value is None:
        return EnvInherit()
    if not isinstance(value, dict):
        raise ConfigError(path, "expected a mapping with `drop` and `drop_dotenv`")
    unknown = sorted(str(k) for k in value if k not in ("drop", "drop_dotenv"))
    if unknown:
        raise ConfigError(path, f"unknown key {unknown[0]!r}; expected `drop` and `drop_dotenv`")
    default = EnvInherit()
    return EnvInherit(
        drop=_string_list(value["drop"], f"{path}.drop") if value.get("drop") is not None else default.drop,
        drop_dotenv=(_string_list(value["drop_dotenv"], f"{path}.drop_dotenv")
                     if value.get("drop_dotenv") is not None else default.drop_dotenv),
    )


_ENV_PATTERN = re.compile(r"[A-Za-z0-9_*?\[\]!-]{1,128}")


def _string_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(path, "expected a list of strings")
    return list(value)


def brief_files(value: Any, path: str) -> list[str]:
    """`brief:` is one repo path or a list of them."""
    if value is None:
        return []
    return [value] if isinstance(value, str) else _string_list(value, path)


def _build_agent(name: str, data: Any, path: str) -> AgentSpec:
    if not isinstance(data, dict):
        raise ConfigError(path, "expected a mapping")
    command = data.get("command")
    if isinstance(command, str):
        command = shlex.split(command)
    if not command:
        raise ConfigError(_join(path, "command"), "missing")
    return AgentSpec(
        name=name,
        enabled=bool(_optional(data, "enabled", path, bool, True)),
        command=_string_list(command, _join(path, "command")),
        base_args=_string_list(data.get("base_args", []), _join(path, "base_args")),
        permission_mode=_optional(data, "permission_mode", path, str, None),
        models=dict(_optional(data, "models", path, dict, {})),
        effort_arg=_string_list(data.get("effort_arg", []), _join(path, "effort_arg")),
        max_turns=_optional(data, "max_turns", path, int, None),
        profile=_optional(data, "profile", path, str, None),
        skill_loading=_optional(data, "skill_loading", path, str, "native"),
        prices=_build_prices(data.get("prices"), _join(path, "prices")),
    )


def _build_prices(raw: Any, path: str) -> dict[str, dict[str, float]]:
    """`{model id: {input, output, cache_read, cache_write_5m, cache_write_1h}}` in USD per 1M tokens."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(path, "expected a mapping of model id to prices")
    out: dict[str, dict[str, float]] = {}
    for model, row in raw.items():
        here = _join(path, str(model))
        if not isinstance(row, dict):
            raise ConfigError(here, "expected a mapping of price names to numbers")
        for key, value in row.items():
            if key not in PRICE_KEYS:
                raise ConfigError(_join(here, str(key)), f"unknown price; known: {', '.join(PRICE_KEYS)}")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ConfigError(_join(here, str(key)), "expected a number of USD per 1M tokens, 0 or more")
        out[str(model)] = {str(k): float(v) for k, v in row.items()}
    return out


def _step_type(raw: dict, path: str) -> str:
    """D4: explicit `type`, else `steps` -> group, else skill/prompt/role -> agent, else gates."""
    explicit = raw.get("type")
    if explicit is not None:
        return str(explicit)
    if "steps" in raw:
        return "group"
    if any(raw.get(k) for k in ("skill", "prompt", "role")):
        return "agent"
    if "gates" in raw:
        return "gate"
    raise ConfigError(path, "cannot tell the step type: give it `type`, `steps`, `skill`/`prompt`/`role`, or `gates`")


class _Builder:
    def __init__(self, origins: dict[str, str]) -> None:
        self.origins = origins

    def steps(self, value: Any, path: str) -> list[Step]:
        """Build a step list found at `path`; steps pulled in by reference keep their source path."""
        if not isinstance(value, list):
            raise ConfigError(path, "expected a list of steps")
        base = self.origins.get(path, path)
        return [self.step(raw, f"{base}[{i}]") for i, raw in enumerate(value)]

    def step(self, raw: Any, path: str, default_id: str | None = None) -> Step:
        if not isinstance(raw, dict):
            raise ConfigError(path, "a step must be a mapping")
        for key in raw:
            if key not in STEP_KEYS:
                raise ConfigError(_join(path, str(key)), "unknown step key")
        step_type = _step_type(raw, path)
        review = None
        if raw.get("review") not in (None, False):
            review = self.step(raw["review"], _join(path, "review"), default_id="review")
        nested = self.steps(raw["steps"], _join(path, "steps")) if "steps" in raw else []
        fix = self.step(raw["fix"], _join(path, "fix"), default_id="fix") if raw.get("fix") else None
        sync = self.step(raw["sync"], _join(path, "sync"), default_id="sync") if raw.get("sync") else None
        step_id = raw.get("id") or raw.get("skill") or default_id or step_type
        if not isinstance(raw.get("reconciles", False), bool):
            raise ConfigError(_join(path, "reconciles"), "reconciles must be true or false")
        return Step(
            id=str(step_id),
            type=step_type,
            skill=raw.get("skill"),
            role=raw.get("role"),
            prompt=raw.get("prompt"),
            output=raw.get("output"),
            when=raw.get("when"),
            unless=raw.get("unless"),
            repeat=raw.get("repeat"),
            next=raw.get("next"),
            gates=raw.get("gates"),
            fix_loop=bool(raw.get("fix_loop", False)),
            gates_first=bool(raw.get("gates_first", False)),
            review=review,
            commit=raw.get("commit", False),
            message=raw.get("message"),
            foreach=raw.get("foreach"),
            expect=raw.get("expect"),
            model=raw.get("model"),
            wait_for=raw.get("wait_for"),
            checks=list(raw.get("checks") or []),
            on_fail=raw.get("on_fail"),
            steps=nested,
            lanes=_string_list(raw.get("lanes", []), _join(path, "lanes")),
            tdd=bool(raw.get("tdd", True)),
            small_plan=raw.get("small_plan"),
            batch=raw.get("batch"),
            brief=brief_files(raw.get("brief"), _join(path, "brief")),
            task_briefs=dict(raw.get("task_briefs") or {}),
            name=raw.get("name"),
            blocks_on_findings=bool(raw.get("blocks_on_findings", False)),
            reconciles=raw.get("reconciles", False),
            forbid=_string_list(raw.get("forbid", []), _join(path, "forbid")),
            fix=fix,
            sync=sync,
            human_paths=_string_list(raw.get("human_paths", []), _join(path, "human_paths")),
            path=path,
        )

    def phases(self, data: dict) -> dict[str, Phase]:
        out: dict[str, Phase] = {}
        for name, raw in data.items():
            path = f"phases.{name}"
            if not isinstance(raw, dict):
                raise ConfigError(path, "expected a mapping")
            model = raw.get("model")
            if model is not None and not isinstance(model, (str, dict)):
                raise ConfigError(_join(path, "model"), "expected a model name or a role map")
            out[name] = Phase(
                name=name,
                agent=raw.get("agent"),
                model=model,
                skip_if=raw.get("skip_if"),
                steps=self.steps(raw.get("steps", []), _join(path, "steps")),
                needs_approval=raw.get("needs_approval", True) is not False,  # anything but an explicit false keeps the human
                resolve=raw.get("resolve") is True,
                approval_note=raw.get("approval_note"),
            )
        return out


def _build_gates(data: dict) -> dict[str, Gate]:
    out: dict[str, Gate] = {}
    for name, raw in data.items():
        path = f"gates.{name}"
        if not isinstance(raw, dict):
            raise ConfigError(path, "expected a mapping")
        out[name] = Gate(
            name=name,
            run=_require(raw, "run", path, str),
            cwd=_optional(raw, "cwd", path, str, None),
            when_exists=_optional(raw, "when_exists", path, str, None),
            baseline=raw.get("baseline"),  # kept as written: `validate` refuses anything but red or green
            agent_check=_optional(raw, "agent_check", path, bool, False),
        )
    return out


def _check_format(raw: dict) -> None:
    """A pack says which format it is written in (`version`) and which engines may run it (`engine`)."""
    version = _require(raw, "version", "", int)
    if isinstance(version, bool) or version != pack.PACK_VERSION:
        raise ConfigError("version", f"pack format {version}; this engine reads format {pack.PACK_VERSION}")
    spec = _require(raw, "engine", "", str)
    try:
        ok = pack.engine_satisfies(spec)
    except pack.PackError as exc:
        raise ConfigError("engine", str(exc)) from exc
    if not ok:
        raise ConfigError("engine",
                          f"engine: '{spec}' does not include this engine; this engine is {pack.ENGINE_VERSION}")


def _skills_dir(value: str) -> str:
    """`project.skills_dir` in one spelling (`./skills/` is `skills`), so the user-only-skill guard matches every
    read of it; empty stays empty for the validator to refuse."""
    value = value.strip()
    return posixpath.normpath(value) if value else ""


def _work_source(raw: dict) -> WorkSource | None:
    block = _optional(raw, "work_source", "", dict, None)
    if block is None:
        return None
    kind = _require(block, "kind", "work_source", str)
    if kind == "capability_map":
        return WorkSource(kind=kind, path=_require(block, "path", "work_source", str))
    return WorkSource(kind=kind, path=_optional(block, "path", "work_source", str, ""))


def _skills_map(raw: dict) -> dict[str, str]:
    block = _optional(raw, "skills", "", dict, {})
    return {str(k): str(v) for k, v in _optional(block, "map", "skills", dict, {}).items()}


MAX_DEPTH = 64  # a config file nested deeper than this is refused: no pack needs it, and every pass below recurses


def _no_cycle(node: Any, path: Path, ancestors: set[int], done: set[int], depth: int = 0) -> None:
    """Refuse a value that contains itself (a YAML anchor used inside its own node) or nests past `MAX_DEPTH`;
    each shared node (an anchor used twice) is walked once."""
    if not isinstance(node, (dict, list)) or id(node) in done:
        return
    if id(node) in ancestors:
        raise ConfigError("", f"{path} holds a YAML anchor that refers to itself")
    if depth > MAX_DEPTH:
        raise ConfigError("", f"{path} is nested more than {MAX_DEPTH} levels deep")
    ancestors.add(id(node))
    for child in node.values() if isinstance(node, dict) else node:
        _no_cycle(child, path, ancestors, done, depth + 1)
    ancestors.discard(id(node))
    done.add(id(node))


def _read_yaml(path: Path) -> Any:
    """`path` parsed. A file that can't be read, isn't YAML or holds a value that contains itself is a `ConfigError`
    naming it, never a traceback."""
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        _no_cycle(loaded, path, set(), set())
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError("", f"cannot read {path}: {exc}") from exc
    except RecursionError as exc:
        raise ConfigError("", f"{path} is nested too deeply to read") from exc
    return loaded


def _read_mapping(path: Path) -> dict:
    loaded = _read_yaml(path)
    if not isinstance(loaded, dict):
        raise ConfigError("", f"{path} must hold a mapping at the top level")
    return loaded


def _merge_layers(project: dict, path: Path, user: bool) -> tuple[dict, dict, Path | None]:
    """`(merged, origins, user file or None)` for the stack engine defaults -> the starter the pack names (if any)
    -> the pack -> the user's own file.

    The user file is read only when `user` is true and it exists, and it is checked (`layers.check_user`) against
    the merged layers below it before it is merged: it never reaches `merge` unchecked."""
    try:
        stack = [*layers.base_layers(project.get("starter")), layers.Layer("project", project, path)]
        merged, origins = layers.merge(stack)
        if not user:
            return merged, origins, None
        try:
            user_path = layers.user_config_path()
        except RuntimeError:  # no home directory to look in: there is no user file
            return merged, origins, None
        try:
            present = user_path.exists()
        except OSError as exc:  # an unreadable parent directory: say which file, not a traceback
            raise ConfigError("", f"cannot read {user_path}: {exc}") from exc
        if not present:
            return merged, origins, None
        data = _read_yaml(user_path)
        data = {} if data is None else data  # an empty file sets nothing
        layers.check_user(data, merged, user_path)
        merged, origins = layers.merge([*stack, layers.Layer("user", data, user_path)])
    except layers.LayerError as exc:
        raise ConfigError(exc.path, exc.message) from exc
    return merged, origins, user_path


def load_config(path: Path, *, layered: bool = True, user: bool = True) -> Config:
    """Merge the layers, resolve `@` references and build the dataclasses.

    The layers are the engine defaults (`agentflow/defaults/config.yaml`), the starter the pack names with `starter:`
    (`agentflow/starters/<name>/config.yaml`), the pack at `path` and the user's own file
    (`layers.user_config_path`), bottom-up. `version` and `engine` are checked on the pack file alone.
    `layered=False` loads the pack file alone, as before the layers; `user=False` leaves the user file unread.
    An error at a key the user file set names that file."""
    path = Path(path)
    project = _read_mapping(path)
    _check_format(project)
    user_path: Path | None = None
    if layered:
        merged, origins, user_path = _merge_layers(project, path, user)
    else:
        merged, origins = copy.deepcopy(project), {key: "project" for key in layers.leaf_keys(project)}
    try:
        return _build(merged, origins, path, _switched_off(project, merged) if layered else ())
    except ConfigError as exc:
        if user_path is not None and exc.path and layers.origin_of(origins, exc.path) == "user":
            raise ConfigError(f"{user_path}: {exc.path}", exc.message) from exc
        raise


def _switched_off(project: dict, merged: dict) -> tuple[str, ...]:
    """The obligation ids a layer switched off, for `validate` to report (the merge drops a `null`, and a `null`
    `policy` or `obligations` drops them all): each engine default missing from the merged obligations, then the
    floor when the pack file sets it to `null`."""
    def entries(data: Any) -> dict:
        policy = data.get("policy") if isinstance(data, dict) else None
        found = policy.get("obligations") if isinstance(policy, dict) else None
        return found if isinstance(found, dict) else {}

    engine, kept = entries(layers.base_layers(project.get("starter"))[0].data), entries(merged)
    ids = [key for key in engine if key != FLOOR_ID and key not in kept]
    if FLOOR_ID in entries(project) and entries(project)[FLOOR_ID] is None:
        ids.append(FLOOR_ID)
    return tuple(ids)


def _build_policy(policy: dict, disabled: tuple[str, ...]) -> Policy:
    """The obligations, in layer order, then the engine floor, whatever the layers say. An evidence item that does
    not parse becomes `UNRECOGNISED`, which `conformance.evaluate` never counts as met, so a typo fails closed;
    `validate` reports it from the text."""
    entries = _optional(policy, "obligations", "policy", dict, {})
    out: dict[str, Obligation] = {}
    for key, entry in entries.items():
        path = f"policy.obligations.{key}"
        if not isinstance(key, str):
            raise ConfigError("policy.obligations", f"an obligation id is text, got {key!r}")
        if key == FLOOR_ID:
            continue
        if not isinstance(entry, dict):
            raise ConfigError(path, "expected a mapping with `require` (and optionally `when` and `lanes`)")
        parsed = (parse_evidence(item) for item in _string_list(entry.get("require", []), f"{path}.require"))
        out[key] = Obligation(
            id=key,
            require=tuple(UNRECOGNISED if item is None else item for item in parsed),
            when=_optional(entry, "when", path, str, None),
            lanes=tuple(_string_list(entry.get("lanes", []), f"{path}.lanes")),
        )
    out[FLOOR_ID] = Obligation(FLOOR_ID, (Evidence("gates", "verify"),))
    return Policy(enforce=_optional(policy, "enforce", "policy", str, "report"), obligations=out, disabled=disabled)


def _build(merged: dict, layer_origins: dict, path: Path, disabled: tuple[str, ...] = ()) -> Config:
    origins: dict[str, str] = {}
    raw = _resolve(merged, "", merged, (), origins)

    project = _require(raw, "project", "", dict)
    defaults = _require(raw, "defaults", "", dict)
    effort = _require(raw, "effort", "", dict)
    context = _require(raw, "context", "", dict)
    memory = _optional(raw, "memory", "", dict, {})
    issues = _optional(raw, "issues", "", dict, {})
    policy = _optional(raw, "policy", "", dict, {})
    guardrails = _require(raw, "guardrails", "", dict)
    recovery = _optional(raw, "recovery", "", dict, {})
    agents = _require(raw, "agents", "", dict)
    gate_sets = _require(raw, "gate_sets", "", dict)
    docs = dict(_optional(project, "docs", "project", dict, {}))
    builder = _Builder(origins)

    return Config(
        version=_require(raw, "version", "", int),
        project=Project(
            base_branch=_require(project, "base_branch", "project", str),
            fork_from=_require(project, "fork_from", "project", str),
            branch=_require(project, "branch", "project", str),
            worktrees_dir=_optional(project, "worktrees_dir", "project", str, None),
            docs=docs,
            commit_trailer=_require(project, "commit_trailer", "project", str),
            skills_dir=_skills_dir(_optional(project, "skills_dir", "project", str, DEFAULT_SKILLS_DIR)),
            spec_file=_optional(docs, "spec_file", "project.docs", str, DEFAULT_SPEC_FILE),
            done_status=_optional(docs, "done_status", "project.docs", str, DEFAULT_DONE_STATUS),
        ),
        agents={name: _build_agent(name, spec, f"agents.{name}") for name, spec in agents.items()},
        defaults=Defaults(
            agent=_require(defaults, "agent", "defaults", str),
            step_timeout_min=_optional(defaults, "step_timeout_min", "defaults", int, 45),
            fix_attempts=_optional(defaults, "fix_attempts", "defaults", int, 3),
            run_budget_usd=_optional(defaults, "run_budget_usd", "defaults", (int, float), 25),
            gate_timeout_min=_optional(defaults, "gate_timeout_min", "defaults", (int, float), 30),
            env=_string_map(defaults.get("env"), "defaults.env"),
            task_budget_usd=_optional(defaults, "task_budget_usd", "defaults", (int, float), 2),
            env_inherit=_env_inherit(defaults.get("env_inherit")),
            run_budget_hard_usd=_optional(defaults, "run_budget_hard_usd", "defaults", (int, float), 65),
        ),
        effort=EffortPolicy(
            default=_require(effort, "default", "effort", str),
            allowed=_string_list(effort.get("allowed"), "effort.allowed"),
            sizing=_optional(effort, "sizing", "effort", bool, True),
        ),
        context=ContextPolicy(
            autocompact_pct=_require(context, "autocompact_pct", "context", int),
            autocompact_env=_optional(context, "autocompact_env", "context", str, "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"),
            rollover_pct=_require(context, "rollover_pct", "context", int),
            max_rollovers=_require(context, "max_rollovers", "context", int),
            max_compactions=_optional(context, "max_compactions", "context", int, 3),
            context_window_tokens=_optional(context, "context_window_tokens", "context", (int, str), 200000),
            default_window_tokens=_optional(context, "default_window_tokens", "context", int, 200000),
            progress_file=_require(context, "progress_file", "context", str),
            gate_output_lines=_require(context, "gate_output_lines", "context", int),
        ),
        memory=MemoryPolicy(
            lessons_max_tokens=_optional(memory, "lessons_max_tokens", "memory", int, 1500),
            digest_tokens=_optional(memory, "digest_tokens", "memory", int, 15000),
            retro_every_days=_optional(memory, "retro_every_days", "memory", int, 7),
        ),
        issues=IssuePolicy(
            in_progress_label=_optional(issues, "in_progress_label", "issues", str, "agentflow:in-progress"),
            comment=_optional(issues, "comment", "issues", bool, True),
            epic_labels=tuple(_string_list(issues.get("epic_labels", ["epic"]), "issues.epic_labels")),
            sync_label=_optional(issues, "sync_label", "issues", str, ""),
            sync_lane=_optional(issues, "sync_lane", "issues", str, "feature"),
            lane_labels=_string_map(issues.get("lane_labels", {"bug": "bug"}), "issues.lane_labels"),
        ),
        gates=_build_gates(_require(raw, "gates", "", dict)),
        gate_sets={name: _string_list(members, f"gate_sets.{name}") for name, members in gate_sets.items()},
        guardrails=Guardrails(
            forbidden_paths=list(dict.fromkeys([  # the pack's own first; the engine's floor always holds
                *_string_list(guardrails.get("forbidden_paths", []), "guardrails.forbidden_paths"),
                *pack.ENGINE_FORBIDDEN,
            ])),
            reject_trailers=_string_list(guardrails.get("reject_trailers", []), "guardrails.reject_trailers"),
            user_only_skills=_string_list(guardrails.get("user_only_skills", []), "guardrails.user_only_skills"),
        ),
        overlays=_optional(raw, "overlays", "", dict, {}),
        conditions=_optional(raw, "conditions", "", dict, {}),
        lanes=_string_list(_require(raw, "lanes", "", list), "lanes"),
        phases=builder.phases(_require(raw, "phases", "", dict)),
        record_step=builder.step(raw["record_step"], "record_step") if raw.get("record_step") else None,
        phase_approval=_optional(raw, "phase_approval", "", dict, {}),
        checkpoints=_optional(raw, "checkpoints", "", dict, {}),
        hitl=_optional(raw, "hitl", "", dict, {}),
        dashboard=_optional(raw, "dashboard", "", dict, {}),
        raw=raw,
        path=path,
        recovery=Recovery(
            give_up_h=_optional(recovery, "give_up_h", "recovery", (int, float), 24),
            failed_attempts=_optional(recovery, "failed_attempts", "recovery", int, 3),
        ),
        autonomy=_optional(raw, "autonomy", "", dict, {}),
        resolve_step=builder.step(raw["resolve_step"], "resolve_step") if raw.get("resolve_step") else None,
        retro_step=builder.step(raw["retro_step"], "retro_step") if raw.get("retro_step") else None,
        ci=_optional(raw, "ci", "", dict, {}),
        work_source=_work_source(raw),
        skills_map=_skills_map(raw),
        merged=merged,
        layer_origins=layer_origins,
        policy=_build_policy(policy, disabled),
    )


# --------------------------------------------------------------------------- models


def resolve_model(cfg: Config, phase: Phase, step: Step, role: str, overrides: dict[str, str]) -> tuple[str, str]:
    """`(model, source)` for one agent call, first hit wins.

    `cli` (`--model <phase>=<model>`), `step` (`model:` on the step), `phase` (a string) or
    `phase_role` (the role's entry in a map, else its `default`), then `agent_role`
    (`agents.<agent>.models[<role>]`, the phase's agent or `defaults.agent`).
    """
    if phase.name in overrides:
        return overrides[phase.name], "cli"
    if step.model:
        return step.model, "step"
    if isinstance(phase.model, str):
        return phase.model, "phase"
    if isinstance(phase.model, dict):
        if role in phase.model:
            return phase.model[role], "phase_role"
        if "default" in phase.model:
            return phase.model["default"], "phase_role"
    agent_name = phase.agent or cfg.defaults.agent
    agent = cfg.agents.get(agent_name)
    if agent is not None and role in agent.models:
        return agent.models[role], "agent_role"
    raise ValueError(f"no model for role {role!r}: phase '{phase.name}' sets none and agent '{agent_name}' has no entry")


# --------------------------------------------------------------------------- validation


def _label_problem(label: str) -> bool:
    """True when `label` cannot be a GitHub label agentflow passes to `gh` (\"\" is fine: it means off)."""
    return len(label) > 50 or "\n" in label or "," in label or label.startswith("-")


def _same_label(a: str, b: str) -> bool:
    """GitHub compares label names without case."""
    return unicodedata.normalize("NFC", a).casefold() == unicodedata.normalize("NFC", b).casefold()


class _Validator:
    def __init__(self, cfg: Config, repo: Path) -> None:
        self.cfg = cfg
        self.repo = repo
        self.issues: list[Issue] = []
        self.roles = {role for agent in cfg.agents.values() for role in agent.models}

    def add(self, level: Literal["error", "warning"], path: str, message: str) -> None:
        issue = Issue(level, path, message)
        if issue not in self.issues:
            self.issues.append(issue)

    def error(self, path: str, message: str) -> None:
        self.add("error", path, message)

    # ---- conditions

    def condition(self, value: Any, path: str, named: bool = False) -> None:
        """Check a condition reference: a defined name, or an inline check/all/any/not."""
        if isinstance(value, str):
            if value not in self.cfg.conditions:
                self.error(path, f"undefined condition '{value}'")
            return
        if not isinstance(value, dict):
            self.error(path, "a condition is a name or a mapping with check, all, any or not")
            return
        if "check" in value:
            if value["check"] not in BUILTIN_CHECKS:
                self.error(_join(path, "check"), f"unknown check '{value['check']}'")
            if value["check"] == "modules_done" and "from" in value:
                self.error(_join(path, "from"), "modules_done reads work_source")
        elif "all" in value or "any" in value:
            key = "all" if "all" in value else "any"
            members = value[key]
            if not isinstance(members, list) or not members:
                self.error(_join(path, key), f"`{key}` needs a non-empty list of conditions")
            else:
                for i, member in enumerate(members):
                    self.condition(member, f"{path}.{key}[{i}]")
        elif "not" in value:
            self.condition(value["not"], _join(path, "not"))
        else:
            self.error(path, "a condition needs one of check, all, any or not")
        if named and "on_unknown" in value and value["on_unknown"] not in ON_UNKNOWN_VALUES:
            self.error(_join(path, "on_unknown"), "on_unknown must be ask_human, agent, true or false")

    def conditions(self) -> None:
        for name, value in self.cfg.conditions.items():
            self.condition(value, f"conditions.{name}", named=True)
        graph = {name: sorted(_condition_names(value)) for name, value in self.cfg.conditions.items()}
        reported: set[str] = set()
        for name in graph:
            cycle = _find_cycle(graph, name)
            if cycle and cycle[0] not in reported:
                reported.update(cycle)
                self.error(f"conditions.{cycle[0]}", f"condition cycle: {' -> '.join(cycle)}")

    # ---- steps

    def skill(self, skill: Any, path: str) -> None:
        if not isinstance(skill, str):
            self.error(path, "skill must be a name")
        elif skill in self.cfg.guardrails.user_only_skills:
            self.error(path, f"'{skill}' is a user-only skill: only the human runs it")
        elif pack.SKILL_NAME.fullmatch(skill) is None or skill in (".", ".."):
            self.error(path, f"'{skill}' is not a skill name: a skill is a directory name, not a path")
        elif pack.skill_file(self.repo, self.cfg.project.skills_dir, skill) is None:
            self.error(path, f"skill '{skill}' has no {self.cfg.project.skills_dir}/{skill}/SKILL.md "
                             "and no bundled skill of that name")

    def prompt(self, prompt: Any, path: str) -> None:
        if not isinstance(prompt, str) or pack.prompt_path(self.cfg.path, prompt) is None:
            self.error(path, f"prompt template '{prompt}' is not in the pack's prompts/ or the engine's")

    def steps(self, steps: list[Step]) -> None:
        """Validate one step list (a phase) and everything nested in it."""
        self.unique_ids(steps, set())
        ids = _all_step_ids(steps)
        self._walk(steps, ids)

    def unique_ids(self, steps: list[Step], seen: set[str]) -> None:
        """Step ids are unique across the whole phase: nested `steps:` and `review` included."""
        for step in steps:
            if step.id in seen:
                self.error(_join(step.path, "id"), f"duplicate step id '{step.id}'")
            elif SIZER_STEP_ID.fullmatch(step.id):
                self.error(_join(step.path, "id"), f"step id '{step.id}' is reserved: the runner's effort sizers "
                                                   "use `size` and `size-<n>`")
            seen.add(step.id)
            if step.review is not None:
                self.unique_ids([step.review], seen)
            self.unique_ids(step.steps, seen)

    def _walk(self, steps: list[Step], phase_ids: set[str]) -> None:
        for step in steps:
            self.step(step, phase_ids)

    def templates(self) -> None:
        """Every `{placeholder}` in every prompt template must be a known key."""
        for file in pack.prompt_files(self.cfg.path).values():
            try:
                text = file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                self.error(pack.prompt_label(self.cfg.path, file), "is not UTF-8 text")
                continue
            for name in dict.fromkeys(_PLACEHOLDER.findall(text)):
                if name not in PROMPT_PLACEHOLDERS:
                    self.error(pack.prompt_label(self.cfg.path, file), f"unknown placeholder '{{{name}}}'")

    def prompt_drift(self) -> None:
        """The pack's prompt overrides that hold text the engine retired, or copy an engine prompt unchanged."""
        from agentflow import pack_fix  # it imports `Issue` from here

        for issue in pack_fix.findings(self.cfg.path).issues:
            if issue not in self.issues:
                self.issues.append(issue)

    def brief(self, files: list[str], prompt: str | None, path: str, skill: Any = None) -> None:
        """A brief goes in the prompt at `{brief}`: a missing file, a `{brief}` with no file, or a file with no
        `{brief}` would each lose it silently. A step with no `brief:` whose skill resolves (`pack.skill_file`)
        has the skill's text as its brief, so a `{brief}` is used then."""
        for name in files:
            if not (self.repo / name).is_file():
                self.error(path, f"brief '{name}' is not a file in this repository")
        template = pack.prompt_path(self.cfg.path, prompt) if prompt else None
        if template is None:
            if files:
                self.error(path, "a brief needs a prompt template with a {brief} placeholder")
            return
        used = "{brief}" in template.read_text(encoding="utf-8")
        label = pack.prompt_label(self.cfg.path, template)
        if used and not files:
            if pack.skill_file(self.repo, self.cfg.project.skills_dir, skill) is None:
                self.error(path, f"{label} has a {{brief}} placeholder, so this step must name the file")
        elif files and not used:
            self.error(path, f"{label} has no {{brief}} placeholder, so this brief would never be sent")

    def step(self, step: Step, phase_ids: set[str]) -> None:
        p = step.path
        self.brief(step.brief, step.prompt, _join(p, "brief"), step.skill)
        for name in step.task_briefs.values():
            if not (self.repo / name).is_file():
                self.error(_join(p, "task_briefs"), f"brief '{name}' is not a file in this repository")
        if step.type not in STEP_TYPES:
            self.error(_join(p, "type"), f"unknown step type '{step.type}'")
        if step.skill is not None:
            self.skill(step.skill, _join(p, "skill"))
        elif step.type == "agent" and step.tdd:
            self.error(p, "an agent step maps to one skill: name it, or set `tdd: false` for a step no skill owns")
        if step.prompt is not None:
            self.prompt(step.prompt, _join(p, "prompt"))
        if step.role is not None and step.role not in self.roles:
            self.error(_join(p, "role"), f"role '{step.role}' is in no agent's models map")
        for key in ("when", "unless"):
            value = getattr(step, key)
            if value is not None:
                self.condition(value, _join(p, key))
        if step.repeat is not None:
            self.repeat(step.repeat, _join(p, "repeat"))
        for i, branch in enumerate(step.next or []):
            self.branch(branch, f"{p}.next[{i}]", phase_ids)
        if step.gates is not None:
            self.gates(step.gates, _join(p, "gates"))
        for name in step.checks:
            if name not in DOC_CHECK_NAMES:
                self.error(_join(p, "checks"), f"'{name}' is not a doc check ({', '.join(DOC_CHECK_NAMES)})")
        if step.checks:
            for key in ("idea", "decisions"):
                if not self.cfg.project.docs.get(key):
                    self.error(_join(p, "checks"), f"doc checks read project.docs.{key}, which this pack does not set")
        if step.expect is not None and step.expect not in EXPECT_VALUES:
            self.error(_join(p, "expect"), f"expect must be one of {', '.join(EXPECT_VALUES)}")
        if step.on_fail is not None and step.on_fail not in ON_FAIL_VALUES:
            self.error(_join(p, "on_fail"), f"on_fail must be one of {', '.join(ON_FAIL_VALUES)}")
        if step.gates_first and (step.type != "agent" or not step.gates):
            self.error(_join(p, "gates_first"), "gates_first needs an agent step with `gates`")
        if step.commit not in (True, False, "per_task"):
            self.error(_join(p, "commit"), "commit must be true, false or per_task")
        for lane in step.lanes:
            if lane not in self.cfg.lanes:
                self.error(_join(p, "lanes"), f"lane '{lane}' is not in `lanes`")
        if step.type == "group" and not step.steps:
            self.error(_join(p, "steps"), "a group needs at least one step")
        if step.type == "checkpoint":
            for label in (step.id, step.name):
                if isinstance(label, str) and label.endswith("_approved"):
                    self.error(p, f"'{label}' looks like a phase approval; a phase approval cannot be configured")
        if step.small_plan is not None:
            self.small_plan(step.small_plan, _join(p, "small_plan"))
        if step.batch is not None:
            self.batch(step.batch, _join(p, "batch"), step)
        if step.review is not None:
            self.step(step.review, phase_ids)
        if step.type == "ci" and step.fix is None:
            self.error(_join(p, "fix"), "a ci step needs `fix`, the session that fixes a failed check")
        if step.fix is not None:
            if step.type != "ci":
                self.error(_join(p, "fix"), "only a ci step has `fix`")
            self.step(step.fix, phase_ids)
        if step.sync is not None:
            if step.type != "finish":
                self.error(_join(p, "sync"), "only a finish step has `sync`")
            self.step(step.sync, phase_ids)
        if step.human_paths and step.type == "finish":
            self.error(_join(p, "human_paths"), "`human_paths` belongs on the `sync` session, not on `finish`")
        self._walk(step.steps, phase_ids)

    def repeat(self, repeat: Any, path: str) -> None:
        if not isinstance(repeat, dict):
            self.error(path, "repeat must be a mapping with until and max_rounds")
            return
        rounds = repeat.get("max_rounds")
        if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 1:
            self.error(_join(path, "max_rounds"), "repeat needs max_rounds, a whole number of at least 1")
        if "until" not in repeat:
            self.error(_join(path, "until"), "repeat needs `until`, a condition")
        else:
            self.condition(repeat["until"], _join(path, "until"))

    def branch(self, branch: Any, path: str, phase_ids: set[str]) -> None:
        if not isinstance(branch, dict):
            self.error(path, "a branch is a mapping with when and goto")
            return
        if "when" in branch:
            self.condition(branch["when"], _join(path, "when"))
        goto = branch.get("goto")
        if goto not in phase_ids:
            self.error(_join(path, "goto"), f"goto '{goto}' is not a step of this phase")

    def gates(self, gates: Any, path: str) -> None:
        names = [gates] if isinstance(gates, str) else gates
        if not isinstance(names, list):
            self.error(path, "gates is a gate-set name or a list of gate names")
            return
        for name in names:
            if name not in self.cfg.gates and name not in self.cfg.gate_sets:
                self.error(path, f"'{name}' is neither a gate nor a gate set")

    def small_plan(self, small: Any, path: str) -> None:
        if not isinstance(small, dict):
            self.error(path, "small_plan must be a mapping")
            return
        if "when" in small:
            self.condition(small["when"], _join(path, "when"))
        if "skill" in small:
            self.skill(small["skill"], _join(path, "skill"))
        if "prompt" in small:
            self.prompt(small["prompt"], _join(path, "prompt"))
            self.brief(brief_files(small.get("brief"), _join(path, "brief")), small["prompt"], _join(path, "brief"),
                       small.get("skill"))

    def batch(self, batch: Any, path: str, step: Step) -> None:
        if not isinstance(batch, dict):
            self.error(path, "batch must be a mapping")
            return
        if step.foreach != "plan_task":
            self.error(path, "only a `foreach: plan_task` step has `batch`")
        for key, low in (("max_tasks", 2), ("max_files", 0)):
            value = batch.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < low):
                self.error(_join(path, key), f"{key} must be a whole number of at least {low}")
        if "prompt" in batch:
            self.prompt(batch["prompt"], _join(path, "prompt"))

    # ---- sections

    def project(self) -> None:
        docs = self.cfg.project.docs
        for key in REQUIRED_DOCS:
            if not isinstance(docs.get(key), str) or not docs[key].strip():
                self.error(f"project.docs.{key}", f"project.docs.{key} is required: the directory of the {key}")
        for key in ("idea", "decisions", "glossary"):
            if key in docs and not isinstance(docs[key], str):
                self.error(f"project.docs.{key}", "must be a path")
        if "capability_map" in docs:
            self.error("project.docs.capability_map", "the module list moved to `work_source: {kind: capability_map, "
                       "path: ...}`; nothing reads project.docs.capability_map")
        spec_file = self.cfg.project.spec_file
        if "{module}" not in spec_file or "/" in spec_file:
            self.error("project.docs.spec_file", "spec_file is a file name holding {module}, such as '{module}.md'")
        else:
            try:
                spec_file.format(module="m")
            except (KeyError, IndexError, ValueError, AttributeError) as exc:
                self.error("project.docs.spec_file", f"spec_file cannot be filled in ({type(exc).__name__}: {exc}): "
                           "{module} is its only placeholder; write a literal brace as {{ or }}")
        try:
            re.compile(self.cfg.project.done_status, re.IGNORECASE)
        except re.error as exc:
            self.error("project.docs.done_status", f"project.docs.done_status is not a valid regex ({exc}): it is "
                       "matched against each line of a spec's header")
        worktrees_dir = self.cfg.project.worktrees_dir
        if worktrees_dir is not None and (posixpath.normpath(worktrees_dir.strip() or ".") == "."
                                          or posixpath.isabs(worktrees_dir)
                                          or ".." in posixpath.normpath(worktrees_dir).split("/")):
            self.error("project.worktrees_dir", "worktrees_dir is a directory inside the repository, relative to its "
                       "root and without '..'; leave it unset to keep worktrees in the state dir")
        skills_dir = self.cfg.project.skills_dir
        if skills_dir in ("", ".") or posixpath.isabs(skills_dir) or ".." in skills_dir.split("/"):
            self.error("project.skills_dir", "skills_dir is a directory inside the repository holding "
                       "<skill>/SKILL.md, relative to its root and without '..'")

    def paths(self) -> None:
        """`paths.state_dir`: a path; a state dir inside the checkout that git doesn't ignore is a warning, since
        every run would then show up in `git status` and could be committed."""
        block = self.cfg.raw.get("paths")
        if block is None:
            return
        if not isinstance(block, dict):
            self.error("paths", "must be a mapping")
            return
        value = block.get("state_dir")
        if value is None:
            return
        if not isinstance(value, str) or not value.strip():
            self.error("paths.state_dir", "state_dir must be a path")
            return
        root = pack.checkout_root(self.repo)
        target = pack.resolve_state_dir(value, root)
        if not target.is_relative_to(root):
            return
        relative = target.relative_to(root).as_posix()
        try:
            ignored = subprocess.run(["git", "check-ignore", "-q", f"{relative}/"], cwd=root, capture_output=True)
        except OSError:  # no git to ask: cannot tell whether it is ignored, so say nothing
            return
        if ignored.returncode != 0:
            self.add("warning", "paths.state_dir",
                     f"the state dir {relative}/ is inside the repository and git does not ignore it: "
                     "add it to .gitignore, or leave paths.state_dir unset for a state dir outside the repository")

    def work_source(self) -> None:
        """`work_source:`: `capability_map` at a path inside the repository, or `specs`, which takes no path and
        reads `project.docs.specs`."""
        ws = self.cfg.work_source
        if ws is None:
            return
        if ws.kind not in WORK_SOURCE_KINDS:
            self.error("work_source.kind", "the work source kinds are capability_map and specs")
        if ws.kind == "specs":
            self.specs_source()
            return
        path = ws.path.strip()
        if not path or posixpath.isabs(path) or ".." in posixpath.normpath(path).split("/"):
            self.error("work_source.path", "path is a file inside the repository, relative to its root and without '..'")

    def specs_source(self) -> None:
        """A `specs` work source whose Backlog would be empty without a word: a `path` it never reads, a specs
        directory it cannot list, or no lane to run its modules in."""
        if self.cfg.work_source is not None and self.cfg.work_source.path.strip():
            self.error("work_source.path", "specs reads project.docs.specs; remove path")
        specs = self.cfg.project.docs.get("specs")
        if isinstance(specs, str) and specs.strip():
            target = Path(self.repo) / specs
            try:
                inside = target.resolve().is_relative_to(Path(self.repo).resolve())
            except (OSError, RuntimeError):  # a symlink loop on the way (RuntimeError on Python 3.12)
                inside = False
            if posixpath.isabs(specs) or ".." in posixpath.normpath(specs).split("/"):
                self.error("project.docs.specs", "a specs work source lists project.docs.specs: a directory inside "
                           "the repository, relative to its root and without '..'")
            elif target.is_symlink() or not inside:
                self.error("project.docs.specs", "a specs work source does not list a symlinked directory or one "
                           "outside the repository")
            elif target.exists() and not target.is_dir():
                self.error("project.docs.specs", "a specs work source lists project.docs.specs, which is not a "
                           "directory")
        block = self.cfg.dashboard.get("backlog")
        if not (isinstance(block, dict) and block.get("module_lane")):
            self.add("warning", "dashboard.backlog.module_lane",
                     "a specs work source lists modules only for the module_lane")

    def skills_map(self) -> None:
        """`skills.map`: the skill `agentflow next` names for each kind of step; each one is in `skills_dir` or bundled."""
        block = self.cfg.raw.get("skills")
        if block is None:
            return
        for key in block:
            if key != "map":
                self.error(f"skills.{key}", "unknown key; known: map")
        for key, skill in ((block.get("map") or {}) if isinstance(block.get("map"), dict) else {}).items():
            if key not in SKILLS_MAP_KEYS:
                self.error(f"skills.map.{key}", f"unknown key; known: {', '.join(SKILLS_MAP_KEYS)}")
            else:
                self.skill(skill, f"skills.map.{key}")

    def removed_defaults(self) -> None:
        for key, why in REMOVED_DEFAULTS.items():
            if key in (self.cfg.raw.get("defaults") or {}):
                self.error(f"defaults.{key}", f"defaults.{key} was removed: {why}. Delete the key")

    def env_inherit(self) -> None:
        inherit = self.cfg.defaults.env_inherit
        for index, pattern in enumerate(inherit.drop):
            if not _ENV_PATTERN.fullmatch(pattern):
                self.error(f"defaults.env_inherit.drop[{index}]",
                           "drop holds variable names or globs (letters, digits, `_`, `*`, `?`, `[]`), at most 128 characters")
        for index, rel in enumerate(inherit.drop_dotenv):
            parts = rel.split("/")
            if not rel or rel.startswith("/") or ".." in parts or "\0" in rel or "\\" in rel:
                self.error(f"defaults.env_inherit.drop_dotenv[{index}]",
                           "drop_dotenv holds repository-relative file paths: no leading `/`, no `..`")

    def budgets(self) -> None:
        soft, hard = self.cfg.defaults.run_budget_usd, self.cfg.defaults.run_budget_hard_usd
        sound = True
        for key, value, what in (("run_budget_usd", soft, "the soft limit, which only notifies"),
                                 ("run_budget_hard_usd", hard, "the hard limit, where the run stops")):
            if isinstance(value, bool) or not math.isfinite(value) or not value > 0:
                self.error(f"defaults.{key}", f"{key} ({what}) must be a finite positive number of USD")
                sound = False
        if sound and hard < soft:
            self.error("defaults.run_budget_hard_usd",
                       f"run_budget_hard_usd ({hard}) is below run_budget_usd ({soft}): the hard limit, where the run "
                       f"stops, must be at least the soft limit, which only notifies")

    def agents(self) -> None:
        defaults = self.cfg.defaults.agent
        spec = self.cfg.agents.get(defaults)
        if spec is None:
            self.error("defaults.agent", f"agent '{defaults}' is not defined")
        elif not spec.enabled:
            self.error("defaults.agent", f"agent '{defaults}' is disabled")
        if not self.cfg.defaults.task_budget_usd >= 0:
            self.error("defaults.task_budget_usd", "task_budget_usd must be 0 (off) or a positive number of USD")
        self.budgets()
        self.env_inherit()
        if not self.cfg.defaults.gate_timeout_min > 0:
            self.error("defaults.gate_timeout_min", "gate_timeout_min must be a positive number of minutes")
        for name, agent in self.cfg.agents.items():
            path = f"agents.{name}"
            if agent.effort_arg and not any("{effort}" in arg for arg in agent.effort_arg):
                self.error(_join(path, "effort_arg"), "effort_arg must contain {effort}: every call passes its level")
            for arg in agent.base_args:
                if arg.split("=", 1)[0] in PERMISSION_FLAGS:
                    self.error(_join(path, "base_args"), f"'{arg}' is not allowed: tool rules come from .claude/settings*.json")
            if agent.permission_mode is not None and agent.permission_mode not in PERMISSION_MODES:
                self.error(_join(path, "permission_mode"),
                           f"unknown permission_mode '{agent.permission_mode}': use one of {', '.join(PERMISSION_MODES)}")

    def gate_definitions(self) -> None:
        confirmed = self.cfg.raw.get("gates_confirmed")
        if confirmed is not None and not isinstance(confirmed, bool):
            self.error("gates_confirmed", "gates_confirmed must be true or false")
        elif confirmed is not True:
            self.add("warning", "gates_confirmed", "detected or inherited gates are a proposal: check `gates` and "
                     "`gate_sets`, then set `gates_confirmed: true`")
        for name, gate in self.cfg.gates.items():
            path = f"gates.{name}"
            if gate.cwd is not None and (posixpath.isabs(gate.cwd) or ".." in gate.cwd.split("/")):
                self.error(_join(path, "cwd"), "cwd must be a path inside the repository: relative, with no `..` part")
            if gate.agent_check and (gate.cwd is not None or "\n" in gate.run.strip() or "`" in gate.run):
                self.error(_join(path, "agent_check"), "an agent check runs from the worktree root as one line, exactly "
                           "as written: drop `cwd` and keep `run` to one line with no backtick, or turn agent_check off")
            if gate.baseline is None:
                continue
            if gate.baseline not in BASELINES:
                self.error(_join(path, "baseline"), f"baseline must be one of {', '.join(BASELINES)}")
            elif gate.baseline == "red":
                self.add("warning", _join(path, "baseline"),
                         f"`{name}` failed on `{self.cfg.project.base_branch}` when the pack was set up: "
                         "fix it and remove `baseline`, or delete the gate")
        if not self.cfg.gate_sets.get("verify"):
            self.error("gate_sets.verify", "every pack declares the gates a diff must pass: state them under "
                       "`gates:` and list them in `gate_sets.verify` (a `starter:` can supply them)")
        for name, members in self.cfg.gate_sets.items():
            for i, member in enumerate(members):
                if member not in self.cfg.gates:
                    self.error(f"gate_sets.{name}[{i}]", f"'{member}' is not a defined gate")

    def bug_lane(self) -> None:
        """The bug lane's red/green check runs the regression test only when a Bash allow rule covers it."""
        from agentflow.guards import has_bash_allow_rule  # guards imports this module, so the import waits for the call

        if "bug" not in self.cfg.lanes or has_bash_allow_rule(self.repo):
            return
        program = "<test runner>"
        for member in self.cfg.gate_sets.get("verify") or []:
            gate = self.cfg.gates.get(member)
            words = gate.run.split() if gate else []
            if words:
                program = words[0]
                break
        self.add("warning", "lanes", "the bug lane's red/green check runs the regression test only when a Bash allow "
                 f"rule in .claude/settings.json covers it: add a narrow rule such as `Bash({program} *)`")

    def phases(self) -> None:
        cfg = self.cfg
        for name, phase in cfg.phases.items():
            path = f"phases.{name}"
            raw = cfg.raw["phases"][name]
            for key in raw:
                if key in APPROVAL_PHASE_KEYS:
                    self.error(_join(path, key), "use `needs_approval: true|false` to say whether a human approves this phase")
                elif key not in PHASE_KEYS:
                    self.error(_join(path, key), "unknown phase key")
            if not isinstance(raw.get("needs_approval"), bool):
                self.error(_join(path, "needs_approval"), "every phase says `needs_approval: true` or `false`")
            if "resolve" in raw and not isinstance(raw["resolve"], bool):
                self.error(_join(path, "resolve"), "resolve must be true or false")
            if phase.resolve and cfg.resolve_step is None:
                self.error(_join(path, "resolve"), "`resolve: true` needs a top-level `resolve_step`")
            if phase.approval_note is not None:
                self.approval_note(phase, _join(path, "approval_note"))
            self.phase_model(phase, path)
            if phase.skip_if is not None:
                self.condition(phase.skip_if, _join(path, "skip_if"))
            self.steps(phase.steps)
        if cfg.record_step is not None:
            self.step(cfg.record_step, {cfg.record_step.id})
        if cfg.resolve_step is not None:
            self.step(cfg.resolve_step, {cfg.resolve_step.id})
        if cfg.retro_step is not None:
            self.step(cfg.retro_step, {cfg.retro_step.id})
        self.reconcile_placement()
        self.reconcile_steps()
        self.reconcile_order()

    def reconcile_placement(self) -> None:
        """`reconciles: true` goes on a step at the top level of a phase: nested in a group, a `review`, `fix` or
        `sync`, or on `record_step`, `resolve_step` or `retro_step`, it would never be the phase's reconcile step."""
        cfg = self.cfg

        def nested(step: Step) -> list[Step]:
            below = [*step.steps, *(s for s in (step.review, step.fix, step.sync) if s is not None)]
            return [inner for child in below for inner in (child, *nested(child))]

        for phase in cfg.phases.values():
            for top in phase.steps:
                for step in nested(top):
                    if step.reconciles:
                        self.error(_join(step.path, "reconciles"),
                                   "a reconciles step must be at the top level of a phase's `steps`")
        for step in (cfg.record_step, cfg.resolve_step, cfg.retro_step):
            if step is not None and step.reconciles:
                self.error(_join(step.path, "reconciles"), "only a step in a phase's `steps` can reconcile")

    def reconcile_steps(self) -> None:
        """A reconcile step edits the artifact its review found fault with, so it is an agent step that writes."""
        for phase in self.cfg.phases.values():
            for step in phase.steps:
                if not step.reconciles:
                    continue
                path = _join(step.path, "reconciles")
                if step.type != "agent":
                    self.error(path, "a reconciles step must be an agent step")
                if step.role == "review":
                    self.error(path, "a reconciles step edits the artifact, so it cannot have `role: review`")
                if not step.output:
                    self.error(path, "a reconciles step needs an `output`: the artifact it may change")
                if step.commit is not True:
                    self.error(path, "a reconciles step needs `commit: true`")

    def reconcile_order(self) -> None:
        """Per lane: at most one reconcile step in a phase, before the steps that block on findings. And the two
        warnings that mean the step never runs, or that an approval phase has nothing to settle its findings."""
        cfg = self.cfg
        for name, phase in cfg.phases.items():
            path = f"phases.{name}"
            reconciling = [s for s in phase.steps if s.reconciles]
            blocking_any = any(s.blocks_on_findings for s in _walk_steps(phase.steps))
            for lane in cfg.lanes:
                steps = for_lane(phase.steps, lane)
                found = [s for s in steps if s.reconciles]
                if len(found) > 1:
                    self.error(_join(found[1].path, "reconciles"),
                               f"lane '{lane}' has {len(found)} reconciles steps in phase '{name}': at most one")
                for i, step in enumerate(steps):
                    if step.reconciles and any(s.blocks_on_findings for s in steps[:i]):
                        self.error(_join(step.path, "reconciles"),
                                   f"in lane '{lane}' this step comes after a `blocks_on_findings` step: "
                                   "put it before the steps it reconciles")
            if reconciling and phase.needs_approval is not True:
                self.add("warning", _join(reconciling[0].path, "reconciles"),
                         f"phase '{name}' has `needs_approval: false`, so its reconciles step never runs")
            elif reconciling and not blocking_any:
                self.add("warning", _join(reconciling[0].path, "reconciles"),
                         f"phase '{name}' has no `blocks_on_findings` step, so its reconciles step never runs")
            if phase.needs_approval is True and blocking_any and not reconciling:
                self.add("warning", path, f"phase '{name}' needs approval and has `blocks_on_findings` steps but no "
                                          "reconciles step: its findings go straight to the human")

    def lanes(self) -> None:
        """`retro` is the engine's own pipeline (`RETRO_PIPELINE`): a lane of that name would make its runs read as
        retros, which no digest reads and no lesson counts, and a phase of that name would own the retro's stops."""
        for i, lane in enumerate(self.cfg.lanes):
            if lane == RETRO_PIPELINE:
                self.error(f"lanes[{i}]", f"`{RETRO_PIPELINE}` is the engine's retro pipeline, not a lane: "
                           "give this lane another name")
        if RETRO_PIPELINE in self.cfg.phases:  # the retro's step key is `retro/retro`: it would read as this phase's
            self.error(f"phases.{RETRO_PIPELINE}", f"`{RETRO_PIPELINE}` is the engine's retro pipeline, so no phase "
                       "may take its name: give this phase another name")

    def approval_note(self, phase: Phase, path: str) -> None:
        note = phase.approval_note
        if not isinstance(note, dict) or "when" not in note or "human_skill" not in note:
            self.error(path, "approval_note needs `when` (a condition) and `human_skill`")
            return
        self.condition(note["when"], _join(path, "when"))
        if note["human_skill"] not in self.cfg.guardrails.user_only_skills:
            self.error(_join(path, "human_skill"), f"'{note['human_skill']}' is not in guardrails.user_only_skills")

    def phase_model(self, phase: Phase, path: str) -> None:
        agent_name = phase.agent or self.cfg.defaults.agent
        agent = self.cfg.agents.get(agent_name)
        if agent is None:
            self.error(_join(path, "agent"), f"agent '{agent_name}' is not defined")
        elif not agent.enabled:
            where = "model" if phase.model is not None else "agent"
            self.error(_join(path, where), f"agent '{agent_name}' is disabled, so this phase cannot run on it")
        if isinstance(phase.model, dict) and "default" not in phase.model:
            self.error(_join(path, "model"), "a model map by role needs a `default`")

    def effort(self) -> None:
        effort = self.cfg.effort
        for i, level in enumerate(effort.allowed):
            if level not in KNOWN_EFFORT_LEVELS:
                self.error(f"effort.allowed[{i}]", f"unknown effort level '{level}'")
        if any(level not in KNOWN_EFFORT_LEVELS for level in effort.allowed):
            self.error("effort.allowed", f"allowed levels must be within {', '.join(KNOWN_EFFORT_LEVELS)}")
        floor = KNOWN_EFFORT_LEVELS.index("medium")
        if effort.allowed and all(level in KNOWN_EFFORT_LEVELS for level in effort.allowed) and not any(
                KNOWN_EFFORT_LEVELS.index(level) >= floor for level in effort.allowed):
            self.error("effort.allowed", "needs a level at or above medium: define, plan, review and debug steps never run at low")
        if effort.default not in effort.allowed:
            self.error("effort.default", f"default '{effort.default}' is not in effort.allowed")

    def context(self) -> None:
        ctx = self.cfg.context
        window = ctx.context_window_tokens
        if isinstance(window, bool) or not (window == "auto" or isinstance(window, int) and 100_000 <= window <= 1_000_000):
            self.error("context.context_window_tokens", "must be `auto` or a number of tokens from 100000 to 1000000 (Claude Code's auto-compact window)")
        if ctx.max_compactions < 1:
            self.error("context.max_compactions", "must be at least 1: a session that compacted this often is replaced by a fresh one")
        if not 1 <= ctx.autocompact_pct <= 100:
            self.error("context.autocompact_pct", "must be a percentage from 1 to 100")
        if not 1 <= ctx.rollover_pct <= 100:
            self.error("context.rollover_pct", "must be a percentage from 1 to 100")
        elif ctx.rollover_pct <= ctx.autocompact_pct:
            self.error("context.rollover_pct", f"must be above autocompact_pct ({ctx.autocompact_pct}): the rollover is the safety net behind the auto-compact")

    def memory(self) -> None:
        mem = self.cfg.memory
        for key, low, high, note in (("lessons_max_tokens", 0, 4000, " (0 turns lessons off)"),
                                     ("digest_tokens", 1000, 50000, ""),
                                     ("retro_every_days", 0, 90, " (0 means manual only)")):
            value = getattr(mem, key)
            if isinstance(value, bool) or not low <= value <= high:
                self.error(f"memory.{key}", f"must be from {low} to {high}{note}")
        self.lessons()

    def issue_policy(self) -> None:
        pol = self.cfg.issues
        if _label_problem(pol.in_progress_label):
            self.error("issues.in_progress_label", "must be at most 50 characters with no newline and no comma, and "
                       "not start with `-` (`\"\"` turns the label and the start comment off)")
        if not isinstance(pol.comment, bool):
            self.error("issues.comment", "must be true or false")
        for i, name in enumerate(pol.epic_labels):
            if not name.strip() or len(name) > 50 or "\n" in name:
                self.error("issues.epic_labels", f"entry {i} must be a non-empty one-line label of at most 50 characters "
                           "(`[]` means no issue is an epic)")
        self.issue_sync(pol)

    def issue_sync(self, pol: IssuePolicy) -> None:
        """`issues.sync_label`, `sync_lane`, `lane_labels`: which open issues the Backlog shows and their lanes."""
        if _label_problem(pol.sync_label):
            self.error("issues.sync_label", "must be at most 50 characters with no newline and no comma, and "
                       "not start with `-` (`\"\"` turns the sync off)")
        elif pol.sync_label and pol.in_progress_label and _same_label(pol.sync_label, pol.in_progress_label):
            self.error("issues.sync_label", "must differ from issues.in_progress_label, or every synced issue "
                       "would read as taken")
        for name in pol.lane_labels:
            if not name.strip() or _label_problem(name):
                self.error(f"issues.lane_labels.{name}", "an issue label of at most 50 characters with no newline "
                           "and no comma, not starting with `-`")
        if not pol.sync_label:
            return
        lanes = self.cfg.lanes
        if pol.sync_lane not in lanes:
            self.error("issues.sync_lane", f"{pol.sync_lane!r} is not a lane; known: {', '.join(lanes)}")
        for name, lane in pol.lane_labels.items():
            if lane not in lanes:
                self.add("warning", f"issues.lane_labels.{name}",
                         f"{lane!r} is not a lane of this pack, so issues labelled {name!r} use issues.sync_lane")
        block = self.cfg.dashboard.get("backlog")
        block = block if isinstance(block, dict) else {}
        module_lane = block.get("module_lane")
        if not module_lane:
            self.add("warning", "issues.sync_label", "dashboard.backlog.module_lane is not set, so a synced issue "
                     "starts a plain `--issue` run; a lane that needs a module will not work")
            return
        targets = {pol.sync_lane, *(lane for lane in pol.lane_labels.values() if lane in lanes)}
        mapped = self.cfg.work_source is not None and self.cfg.work_source.kind == "capability_map"
        if module_lane in targets and mapped and not isinstance(block.get("module_row"), dict):
            self.error("issues.sync_label", f"a synced issue in the module_lane {module_lane!r} adds a row to the "
                       "capability map, which needs dashboard.backlog.module_row")

    def policy(self) -> None:
        """`policy`: the mode, and each obligation's `when`, `lanes` and evidence, read from the text of the config
        (an item that does not parse is not in the dataclass). An engine default that cannot hold in this pack's
        workflow is an error that says how to switch it off, since the pack, not the engine, owns the workflow."""
        cfg = self.cfg
        pol = cfg.policy
        if pol.enforce not in ENFORCE_MODES:
            self.error("policy.enforce", f"must be one of {', '.join(ENFORCE_MODES)}")
        raw = cfg.raw.get("policy")
        entries = raw.get("obligations") if isinstance(raw, dict) and isinstance(raw.get("obligations"), dict) else {}
        for key in pol.disabled:
            where = f"policy.obligations.{key}"
            if key == FLOOR_ID:
                self.add("warning", where, f"{FLOOR_ID} cannot be removed: it is the obligation `gates:verify`, and it stays")
            else:
                self.add("warning", where, f"the obligation '{key}' is switched off (`null`), so no run shows evidence for it")
        if FLOOR_ID in entries:
            self.add("warning", f"policy.obligations.{FLOOR_ID}",
                     f"{FLOOR_ID} cannot be removed or changed: this entry is ignored")
        lane_steps: dict[str, set[str]] = {}
        for lane in cfg.lanes:
            lane_steps[lane] = _lane_step_ids([s for phase in cfg.phases.values() for s in phase.steps], lane)
        for key, ob in pol.obligations.items():
            if key == FLOOR_ID:
                continue
            where = f"policy.obligations.{key}"
            entry = entries.get(key)
            if not isinstance(entry, dict):
                continue
            engine = cfg.layer_origins.get(("policy", "obligations", key)) == "engine"
            off = f"; set policy.obligations.{key}: null to switch it off" if engine else ""
            for unknown in entry:
                if unknown not in ("require", "when", "lanes"):
                    self.error(_join(where, str(unknown)), "unknown obligation key; an obligation has `require`, `when` and `lanes`")
            if ob.when is not None:
                self.condition(ob.when, _join(where, "when"))
            if not engine:
                for lane in ob.lanes:
                    if lane not in cfg.lanes:
                        self.error(_join(where, "lanes"), f"unknown lane '{lane}'; known: {', '.join(cfg.lanes)}")
            applicable = [lane for lane in (ob.lanes or tuple(cfg.lanes)) if lane in cfg.lanes]
            items = entry.get("require")
            if not isinstance(items, list) or not items:
                self.error(_join(where, "require"), "needs at least one evidence item such as `step:verify`")
                continue
            for i, text in enumerate(items):
                at = f"{where}.require[{i}]"
                evidence = parse_evidence(text) if isinstance(text, str) else None
                if evidence is None:
                    self.error(at, f"'{text}' is not evidence: use gates:<set>, step:<id>, approval:<phase> or expect:red_then_green")
                elif evidence.kind == "gates" and evidence.arg not in cfg.gate_sets:
                    self.error(at, f"'{evidence.arg}' is not a gate set{off}")
                elif evidence.kind == "step" and applicable and not any(evidence.arg in lane_steps[lane] for lane in applicable):
                    self.error(at, f"no step '{evidence.arg}' in the workflow of {', '.join(applicable)}{off}")
                elif evidence.kind == "approval":
                    phase = cfg.phases.get(evidence.arg)
                    if phase is None or not phase.needs_approval:
                        # an engine default only warns: Settings can turn a phase's approval off, and the pack can then
                        # switch the obligation off in the file; a pack-written one is an error
                        self.add("warning" if engine else "error", at,
                                 f"'{evidence.arg}' is not a phase with needs_approval: true{off}")

    def lessons(self) -> None:
        """The pack's lessons file (beside the config): each malformed entry, unknown phase and unknown target is
        a warning, since a bad entry is only left out of prompts. A passed `review_by` is never one, so the date
        cannot turn a clean `validate` red."""
        from agentflow import lessons, signals  # lessons -> hitl -> config: imported here, not at the top
        where = pack.LESSONS_FILE
        found, problems = lessons.read(self.cfg.path.parent / posixpath.basename(where), "L")
        for problem in problems:
            self.add("warning", "lessons", f"{where}: {problem}")
        for lesson in found:
            for phase in lesson.applies:
                if phase not in self.cfg.phases:
                    self.add("warning", "lessons", f"{where}: {lesson.id} applies to {phase!r}, which is not a phase")
            if lesson.target is not None and signals.parse_target(lesson.target) is None:
                self.add("warning", "lessons", f"{where}: {lesson.id} target {lesson.target!r} is not a signal key")

    def approval(self) -> None:
        cfg = self.cfg
        for key in cfg.phase_approval:
            if key not in PHASE_APPROVAL_KEYS:
                self.error(f"phase_approval.{key}", "phase approval cannot be skipped or changed: only refine_prompt and show are configurable")
        refine = cfg.phase_approval.get("refine_prompt")
        if refine is not None:
            self.prompt(refine, "phase_approval.refine_prompt")
        for name in cfg.checkpoints:
            if str(name).endswith("_approved"):
                self.error(f"checkpoints.{name}", "a phase approval cannot be configured as a checkpoint")
        for path in _find_keys(cfg.raw, "auto_if"):
            self.error(path, "auto_if is not allowed: a phase's `needs_approval` says whether a human approves it")

    def autonomy(self) -> None:
        cfg = self.cfg
        for key in cfg.autonomy:
            if key not in AUTONOMY_KEYS:
                self.error(f"autonomy.{key}", "unknown autonomy key")
        if not isinstance(cfg.autonomy.get("answer_recommended", False), bool):
            self.error("autonomy.answer_recommended", "answer_recommended must be true or false")
        human = cfg.autonomy.get("human_question_kinds", [])
        if not isinstance(human, list) or any(kind not in QUESTION_KINDS for kind in human):
            self.error("autonomy.human_question_kinds",
                       f"human_question_kinds must be a list of question kinds: {', '.join(QUESTION_KINDS)}")
        self.non_blocking_severities()
        if "finding_rounds" in cfg.autonomy:
            rounds = cfg.autonomy["finding_rounds"]
            if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 1:
                self.error("autonomy.finding_rounds", "finding_rounds must be a whole number, 1 or more: "
                           "automatic refinement of blocking findings is an engine floor and cannot be switched off")
        kinds = cfg.autonomy.get("resolve_kinds", {})
        if not isinstance(kinds, dict):
            self.error("autonomy.resolve_kinds", "resolve_kinds maps a stop kind to its attempts per step")
            kinds = {}
        for kind, attempts in kinds.items():
            if kind in NEVER_RESOLVED:
                self.error(f"autonomy.resolve_kinds.{kind}", f"a {kind} stop is always a human's")
            if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
                self.error(f"autonomy.resolve_kinds.{kind}", "attempts must be a whole number of at least 1")
        for key in CI_KEYS:
            value = cfg.ci.get(key)
            if value is None and any(step.type == "ci" for phase in cfg.phases.values() for step in phase.steps):
                self.error(f"ci.{key}", "a ci step needs this setting")
            elif value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0):
                self.error(f"ci.{key}", "must be a number, 0 or more")
        for key in cfg.ci:
            if key not in CI_KEYS:
                self.error(f"ci.{key}", "unknown ci key")

    def non_blocking_severities(self) -> None:
        """The floor: `critical`, `high`, `medium`, an empty severity and a missing one always block."""
        if "non_blocking_severities" not in self.cfg.autonomy:
            return
        path = "autonomy.non_blocking_severities"
        listed = self.cfg.autonomy["non_blocking_severities"]
        if not isinstance(listed, list) or not all(isinstance(name, str) for name in listed):
            self.error(path, "non_blocking_severities must be a list of severity names (strings)")
            return
        refused = {*FLOOR_BLOCKING, "", "none", "null"}
        for name in listed:
            if name.strip().lower() in refused:
                self.error(path, f"'{name}' always blocks (engine floor: {', '.join(FLOOR_BLOCKING)}, an empty or "
                                 "missing severity) and cannot be listed as non-blocking")

    def overlays(self) -> None:
        for name, overlay in self.cfg.overlays.items():
            skills = overlay.get("add_skills", {}) if isinstance(overlay, dict) else {}
            for stage, skill in skills.items():
                self.skill(skill, f"overlays.{name}.add_skills.{stage}")

    def dashboard(self) -> None:
        if self.cfg.dashboard.get("host") != "127.0.0.1":
            self.error("dashboard.host", "the dashboard binds 127.0.0.1 only")
        if "backlog" in self.cfg.dashboard:
            self.backlog(self.cfg.dashboard["backlog"])

    def backlog(self, block: Any) -> None:
        """`dashboard.backlog`: which lane runs a map module, each lane's New task fields, a new module's row."""
        path = "dashboard.backlog"
        if not isinstance(block, dict):
            self.error(path, "must be a mapping")
            return
        for key in block:
            if key not in BACKLOG_KEYS:
                self.error(f"{path}.{key}", f"unknown key; known: {', '.join(BACKLOG_KEYS)}")
        lanes = self.cfg.lanes
        if "sync_base" in block and not isinstance(block["sync_base"], bool):
            self.error(f"{path}.sync_base", "must be true or false")
        module_lane = block.get("module_lane")
        if module_lane is not None and module_lane not in lanes:
            self.error(f"{path}.module_lane", f"{module_lane!r} is not a lane; known: {', '.join(lanes)}")
        row = block.get("module_row")
        if row is not None:
            headers = {str(k).strip().lower() for k in row} if isinstance(row, dict) else set()
            if (not isinstance(row, dict) or not all(isinstance(v, str) for v in row.values())
                    or not {"module id", "depends on"} <= headers):
                self.error(f"{path}.module_row", "must map column headers to cell templates (text), "
                           "including the `Module id` and `Depends on` columns")
        labels = block.get("lane_labels")
        if labels is not None and not isinstance(labels, dict):
            self.error(f"{path}.lane_labels", "must map a lane to the words the dashboard shows for it")
        elif labels:
            for lane, label in labels.items():
                where = f"{path}.lane_labels.{lane}"
                if lane not in lanes:
                    self.error(where, f"{lane!r} is not a lane; known: {', '.join(lanes)}")
                elif not isinstance(label, str) or not label.strip():
                    self.error(where, "must be text")
        tasks = block.get("new_task")
        if tasks is None:
            return
        specs = self.cfg.work_source is not None and self.cfg.work_source.kind == "specs"  # a new spec has no row
        mapped = self.cfg.work_source is not None and self.cfg.work_source.kind == "capability_map"
        if not isinstance(tasks, dict):
            self.error(f"{path}.new_task", "must map a lane to its form fields")
            return
        for lane, fields in tasks.items():
            where = f"{path}.new_task.{lane}"
            if lane not in lanes:
                self.error(where, f"{lane!r} is not a lane; known: {', '.join(lanes)}")
            elif not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
                self.error(where, "must be a list of form fields")
            elif unknown := [f for f in fields if f not in BACKLOG_FIELDS]:
                self.error(where, f"unknown field {', '.join(unknown)}; known: {', '.join(BACKLOG_FIELDS)}")
            elif not [f for f in fields if f != "after"]:
                self.error(where, "needs a field to fill in besides `after`")
            elif "new_module" in fields and specs and lane != module_lane:
                self.error(where, "`new_module` belongs to the module_lane")
            elif "new_module" in fields and not specs and (lane != module_lane or not isinstance(row, dict)):
                self.error(where, "`new_module` belongs to the module_lane and needs dashboard.backlog.module_row")
            elif ("issue" in fields and lane == module_lane and mapped and not isinstance(row, dict)):
                self.error(where, "an `issue` card in the module_lane adds a row to the capability map, which needs "
                           "dashboard.backlog.module_row")


def _lane_step_ids(steps: list[Step], lane: str) -> set[str]:
    """The ids of the steps a run of `lane` walks, nested steps (a group's, a `repeat`'s) included."""
    ids: set[str] = set()
    for step in for_lane(steps, lane):
        ids.add(step.id)
        ids |= _lane_step_ids(step.steps, lane)
    return ids


def _walk_steps(steps: list[Step]):
    """Every step of a phase, nested groups and `review` steps included."""
    for step in steps:
        yield step
        if step.review is not None:
            yield from _walk_steps([step.review])
        yield from _walk_steps(step.steps)


def _all_step_ids(steps: list[Step]) -> set[str]:
    ids: set[str] = set()
    for step in steps:
        ids.add(step.id)
        ids |= _all_step_ids(step.steps)
    return ids


def _condition_names(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    names: set[str] = set()
    if isinstance(value, dict):
        for key in ("all", "any"):
            for member in value.get(key) or []:
                names |= _condition_names(member)
        if "not" in value:
            names |= _condition_names(value["not"])
    return names


def _find_cycle(graph: dict[str, list[str]], start: str) -> list[str] | None:
    def visit(node: str, trail: list[str]) -> list[str] | None:
        if node in trail:
            return [*trail[trail.index(node) :], node]
        for nxt in graph.get(node, []):
            found = visit(nxt, [*trail, node])
            if found:
                return found
        return None

    return visit(start, [])


def _find_keys(node: Any, key: str, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            here = _join(path, str(k))
            if k == key:
                found.append(here)
            found += _find_keys(v, key, here)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found += _find_keys(v, key, f"{path}[{i}]")
    return found


def validate(cfg: Config, repo: Path) -> list[Issue]:
    """Return every problem found in `cfg`, checked against the checkout at `repo`."""
    v = _Validator(cfg, Path(repo))
    v.removed_defaults()
    v.project()
    v.paths()
    v.work_source()
    v.skills_map()
    v.agents()
    v.gate_definitions()
    v.bug_lane()
    v.lanes()
    v.conditions()
    v.phases()
    v.effort()
    v.context()
    v.memory()
    v.issue_policy()
    v.policy()
    v.approval()
    v.autonomy()
    v.overlays()
    v.dashboard()
    v.templates()
    v.prompt_drift()
    return v.issues
