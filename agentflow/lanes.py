"""The agent step lifecycle: run one config step through an agent until it is done or needs a human.

`Executor.run_agent_step` goes round one loop per agent call, in this order: budget check (the soft
budget notifies once, the hard one stops), then model and effort (from state; for a new step, the level a
read-only sizer session gave it (`sizing_lane.py`), else `effort.default`), prompt, tree snapshot, the call
(`agent_started` ... `agent_exited`, spend, one `permission_denied` per denial), guards, the
`result.json`, gates and doc checks (`checks:`, which always have the fix loop) with the fix loop,
rollover; and once the result is `done` and the gates pass: the progress check, the commit
(`commit: true`) and `step_finished`. Anything the runner cannot settle stops the run for a human
(`raise_hitl`) and returns "stopped". Everything needed to go on later is in the event log, so a
new `Executor` resumes the step's recorded session.

The agent's only extra directory is the run's workspace (`workspace.py`): before each call the runner
refreshes the input copies there, and after it reads the result and `progress.md` from there only. The
runner commits only on top of the last `HEAD` it recorded (`commit_checks.runner_head`).
"""

from __future__ import annotations

import dataclasses
import json
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, TextIO

from agentflow import brief as brief_mod
from agentflow import lessons as lessons_mod
from agentflow import doubt, gitops, overlays, pack, run_env, safeio, sizing, verification, workspace
from agentflow.agents import (CLAUDE_MIN_VERSION, LEAN_ARGS, NO_USER_SETTINGS, Agent, AgentCall, AgentOutcome, Interruption,
                              ignored_settings, read_only_allow)
from agentflow.autonomy import AUTO_BY, runner_settles
from agentflow.bug_eval import BugEvalLane, exhausted_kind
from agentflow.ci_lane import CILane
from agentflow.commit_checks import RUNNER_COMMITS, runner_head
from agentflow.conditions import Ctx, fmt, skill_text
from agentflow.config import BRIEF_PLACEHOLDERS, EFFORT_BLOCK, PREAMBLE_SLOT, Config, Phase, Step, brief_partial, resolve_model
from agentflow.feature_lane import FeatureLane
from agentflow.flow_control import (
    StepControl,
    current_refine,
    halted,
    refine_note_block,
    refine_subject,
    writes_questions,
)
from agentflow.gates import (GateResult, agent_checks, failure_report, gates_ok, record_results, results_table,
                             resume_report, run_doc_checks, run_gates_reusing)
from agentflow.gates import resolve as resolve_gates
from agentflow.gitops import (CommitRejected, GitError, NothingToCommit, changed_since, commit, commit_artifact,
                              commits_between, git_state_changes, head, outside_only, reconcile_baseline)
from agentflow.guards import Violation, changed_paths, check_step, path_matches, snapshot
from agentflow.hitl import ResultError, StepResult, notify_run, park_hitl, raise_hitl, read_result
from agentflow.route_lane import RouteLane
from agentflow.run_control import wait_for_agents
from agentflow.sizing_lane import SizingLane
from agentflow.sync_lane import SyncLane
from agentflow.state import TERMINAL, Run, workspaces_root

Outcome = Literal["done", "skipped", "stopped"]
Mode = Literal["first", "refine"]

HOLD_POLL_S = 30.0  # short sleeps against the wall clock: a machine asleep must not stretch a wait
HOLD_SLACK_S = 60.0  # past `resetsAt`: clock drift must not spend the first call after a limit
BACKOFF_START_S = 60.0
BACKOFF_MAX_S = 900.0

PREAMBLE = "_preamble.md"
RESULT = "_result.md"
STEP_HEAD = "_step_head.md"
DEFAULT_TEMPLATE = "step.md"
DEFAULT_REFINE = "refine.md"
DEFAULT_ROLE = "build"
EMPTY_SLOT = re.compile(r"^\{(run_facts|code_map|lessons)\}\n\n", re.MULTILINE)  # a step head slot, dropped whole when empty
RESULT_REMINDER = ("When you finish, write your result JSON to `{result_file}` again (the runner removed the last "
                   "one), as the \"When you finish\" section of your instructions says.")
CODE_MAP_LEAD = ("Earlier sessions of this run found where this code lives. Open it at the line given instead of "
                 "searching for it, and check a signature before you rely on it.")
RESOLVER_FINDINGS_SHOWN = 3
RESOLVER_FINDING_MAX = 600
SKILL_LINE = re.compile(r"\b([Uu])se the `([\w.:-]+)` skill")
SKILL_HEADING = re.compile(r"#{1,6}[ \t]+.*`\{skill\}`")
SKILL_BLOCK = ("## The `{skill}` skill\n\nIts full text follows, so you already have it: do not load it.\n\n{brief}")
SKILL_POINTER_BLOCK = "## The `{skill}` skill\n\n{brief}"  # the skill is in the session's brief: `{brief}` points there
FULL_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
BRIEF_DIR = "brief"  # under the run's directory (the runs root, which no agent edits): `<phase>-<role>-<digest12>.md`
BRIEF_FLAGS = ("rules", "skill", "files")
BRIEF_REASON_MAX = 300
BRIEF_LEAD = ("This is a fresh session: the runner could not give this step's earlier session its brief again. "
              "Continue the step from `{progress}` and `{handoff}`: read them first, check the work tree, and do not "
              "redo finished work.")
BRIEF_CARRIED = ("## The message the earlier session was about to get\n\nThe runner was about to send this to the earlier "
                 "session. Whatever it holds (answers, a note, gate output) still applies to this step: act on it.")
RESUMABLE = ("running", "waiting_human")
UNSTARTED_STEP = (None, "pending")
FOOTER = re.compile(r"^\s*Next:\s*`?([\w.:/-]+)`?\s+(?:—|–|--?)\s", re.MULTILINE)
SELF_CHECKS_NONE = ("Do not run the linters, type checkers or other quality gates yourself: the runner's build gates "
                    "run them right after you, and a failure comes back to this session with its output.")
SELF_CHECKS = ("Before you write the result, run each of these checks once, after your last edit. They are the runner's "
               "own build gates for this step: run them from the worktree root exactly as written, one Bash call per "
               "line.\n\n```sh\n{commands}\n```\n\nFix what they report and run the failing one again. If one is "
               "denied, skip it and say so in your summary: do not stop as `blocked` for it. Do not run any other gate "
               "or the whole suite: the runner runs every build gate right after you, and a failure comes back to this "
               "session with its output.")
HANDOFF_LINE = "Read the handoff from the earlier phases first: `{handoff}`."
# What a read-only session (`agents.claude.read_only_roles`) is told, `{read_only_note}` in a prompt: it ends the prompt
# when the template has no slot. Only a session's first prompt carries it; a resumed turn's session already has it.
READ_ONLY_NOTE = (
    "## This session is read-only\n\n"
    "You can read files, search, run `git diff`, `log`, `show`, `status` and `blame`, and write your result file. "
    "Editing the code, running tests, linters or builds, and other commands are denied here by design. A denial is "
    "expected: do not stop as `blocked` for it and do not rephrase the command; carry on with what you can read, and "
    "write your result. Do not write `{progress}`: a read-only step leaves it to the steps that change the code. Judge "
    "whether a test is real by reading it and the code it covers."
)


def footer_next(text: str) -> str | None:
    """The skill named by the last `Next: <skill> — <reason>` footer line in `text`, if any."""
    found = FOOTER.findall(text or "")
    return found[-1] if found else None


# --------------------------------------------------------------------------- prompts


def _template(cfg: Config, name: str) -> str:
    file = pack.prompt_path(cfg.path, name)
    if file is None:
        raise FileNotFoundError(f"prompt '{name}' is not in the pack's prompts/ or the engine's")
    return file.read_text(encoding="utf-8")


def _self_checks(cfg: Config, step: Step, worktree: Path) -> str:
    """The build prompts' `{self_checks}`: the step's `agent_check` gates for the implementer to run, or today's
    "run none" sentence when it has none."""
    gates = agent_checks(cfg, step.gates, worktree)
    if not gates:
        return SELF_CHECKS_NONE
    return SELF_CHECKS.format(commands="\n".join(gate.run.strip() for gate in gates))


def _values(cfg: Config, step: Step, ctx: Ctx, extra: dict) -> dict[str, str]:
    """Placeholder values for one step: the run's context, the step's paths, then `extra`.

    The paths are absolute and inside the run's agent workspace (`workspace.py`), the copies the runner
    refreshes before every call: the agent works in another directory and may write nowhere else."""
    values = dict(ctx.values)
    values["step_id"] = step.id
    run = ctx.run
    if run is not None:
        values["run"] = run.id
        agent_dir = run.agent_dir
    else:
        agent_dir = workspaces_root(ctx.repo) / values.get("run", "run")
    values["run_dir"] = str(agent_dir)
    values["progress"] = str(agent_dir / "progress.md")
    values["handoff"] = str(agent_dir / "handoff.md")
    values["questions"] = str(agent_dir / "questions.md")
    values["run_facts"] = _run_facts(run)
    values["code_map"] = _code_map(run)
    worktree = Path(run.state.get("worktree") or ctx.repo) if run is not None else ctx.repo
    values["self_checks"] = _self_checks(cfg, step, worktree)
    values.update({name: str(value) for name, value in extra.items()})
    return values


def _run_facts(run: Run | None) -> str:
    """What earlier resolver sessions of the run found, as a section of the prompt; "" when there is nothing."""
    facts = [str(f.get("text")) for f in (run.state.get("facts") or []) if f.get("text")] if run is not None else []
    if not facts:
        return ""
    lines = "\n".join(f"- {fact}" for fact in facts)
    return ("## What this run already found\n\nEarlier sessions of this run established these. Check one before "
            f"relying on it; do not diagnose it again.\n\n{lines}")


def _code_map(run: Run | None) -> str:
    """The code earlier sessions of the run mapped, as a section of the prompt; "" when there is nothing."""
    notes = [str(c.get("text")) for c in (run.state.get("code_map") or []) if c.get("text")] if run is not None else []
    if not notes:
        return ""
    return "## Code this run has mapped\n\n" + CODE_MAP_LEAD + "\n\n" + "\n".join(f"- {note}" for note in notes)


def _lessons(cfg: Config, repo: Path, phase: str | None, today: date) -> lessons_mod.Selection:
    """The lessons a step of `phase` carries (engine lessons, then the pack's from the main checkout holding
    `repo`). With `memory.lessons_max_tokens` at 0 nothing is read."""
    if cfg.memory.lessons_max_tokens <= 0:
        return lessons_mod.select([], phase, today, 0)
    return lessons_mod.select(lessons_mod.load(repo), phase, today, cfg.memory.lessons_max_tokens)


def _refine_values(ctx: Ctx, rec: dict) -> dict[str, str]:
    refine = (ctx.run.state.get("refine") if ctx.run is not None else None) or {}
    return {
        "refine_round": str(refine.get("round", "")),
        "refine_note": str(refine.get("note", "")),
        "previous_summary": str(rec.get("result") or "(no summary recorded)"),
        "refine_artifacts": _phase_artifacts(ctx, rec.get("phase")),
    }


def _phase_artifacts(ctx: Ctx, phase: str | None) -> str:
    """The files the phase has changed so far (committed or not), one `- path` line each."""
    run = ctx.run
    row = (run.state["phases"].get(phase) or {}) if run is not None and phase else {}
    sha = row.get("start_sha")
    if not sha:
        return "(not recorded)"
    try:
        paths = changed_since(Path(run.state.get("worktree") or ctx.repo), sha)
    except GitError as exc:
        return f"(could not list them: {exc})"
    return "\n".join(f"- `{path}`" for path in paths) or "(none yet)"


def _read_file_skills(text: str, skills_dir: str, inlined: frozenset[str] = frozenset(),
                      resolve: Callable[[str], str] | None = None) -> str:
    """For agents that cannot load skills by name: point them at the skill file in `skills_dir` instead, or at the
    path `resolve` gives for the name. A skill in `inlined` has its text in the prompt already, so its line stays."""

    def replace(match: re.Match[str]) -> str:
        if match.group(2) in inlined:
            return match.group(0)
        read = "Read" if match.group(1) == "U" else "read"
        path = resolve(match.group(2)) if resolve is not None else f"{skills_dir}/{match.group(2)}/SKILL.md"
        return f"{read} `{path}` and follow it"

    return SKILL_LINE.sub(replace, text)


def _skill_path(repo: Path, skills_dir: str) -> Callable[[str], str]:
    """The path a self-contained prompt's skill line names (`build_prompt`): a bundled skill's file in the engine, by
    its absolute path; a repository skill, or a name that resolves to nothing, `<skills_dir>/<name>/SKILL.md`."""

    def resolve(name: str) -> str:
        found = pack.skill_file(repo, skills_dir, name)
        if found is not None and pack.is_bundled(found):
            return str(found)
        return f"{skills_dir}/{name}/SKILL.md"

    return resolve


def _drop_skill_section(body: str, keep: bool = False) -> str:
    """`body` for a step with no skill: the sections headed by the step's skill (`## The `{skill}` skill`) go, or,
    with `keep` (the step has a brief), only their heading is renamed, so no empty `` skill`` is ever shown."""
    kept: list[str] = []
    skipping = False
    for line in body.split("\n"):
        if line.startswith("#"):
            skipping = SKILL_HEADING.match(line) is not None
            if skipping and keep:
                kept.append(line[:len(line) - len(line.lstrip("#"))] + " The brief")
                skipping = False
                continue
        if not skipping:
            kept.append(line)
    return "\n".join(kept).rstrip()


def _inlined_skill(cfg: Config, step: Step, checkout: Path, body: str) -> str | None:
    """The text of the step's skill when its prompt carries it, else None: a step with no configured `brief:` whose
    skill resolves has the skill's text as its brief. Not when the template (`body`) names a skill of the repository
    itself without a `{brief}` slot (a pack's own wording, which tells the agent to load it). A bundled skill is never
    loadable by name, so it is always carried. Whether the step continues a session that has the text is the caller's
    to check: a session's brief holds the skill whatever the task."""
    if not step.skill or step.brief:
        return None
    found = pack.skill_file(checkout, cfg.project.skills_dir, step.skill)
    owned = found is not None and not pack.is_bundled(found) and f"`{step.skill}` skill" in body
    if "{brief}" in body or not owned:
        return skill_text(cfg, checkout, step.skill)
    return None


def _brief_pointer(skill: str | None, files: bool) -> str:
    """What stands at `{brief}` for the text a session's brief holds: the skill named, the `brief:` files, or both."""
    if skill and files:
        return (f"The `{skill}` skill's full text and the step's brief files are in your system prompt: follow them, "
                "do not load them.")
    if skill:
        return f"The `{skill}` skill's full text is in your system prompt: follow it, do not load it."
    return "The step's brief files are in your system prompt: follow them, do not load them."


def build_prompt(cfg: Config, step: Step, ctx: Ctx, rec: dict, extra: dict, mode: Mode,
                 brief: dict | None = None, self_contained: bool = False) -> str:
    """`_preamble.md` + `_step_head.md` + the step's template (after the refine template in a refine round) +
    `_result.md`.

    The preamble's effort check is always stripped (a pack's own `_preamble.md` copy may still hold it): the
    runner sizes every step before it starts. Placeholders are filled in one pass over the joined text, so
    values that hold braces go in verbatim.

    With `brief` (the session's pin, `build_brief`), the preamble and `_result.md` are in the session's system prompt
    and are left out, and so are the skill and the `brief:` files when the pin says the brief holds them: `{brief}`
    then points there. `step.task_brief` always stays in the prompt.

    With `self_contained` (a call with no skills listing: `Executor._self_contained`), a line that tells the agent to
    use a skill by name points at its file instead, as `skill_loading: read_file` does: the step template's and the
    overlay's lines, before placeholders are filled. The step's own carried skill keeps its line.
    """
    values = _values(cfg, step, ctx, {})
    preamble = EFFORT_BLOCK.sub("", _template(cfg, PREAMBLE))
    values["lessons"] = _lessons(cfg, ctx.repo, extra.get("phase"), date.today()).text.rstrip("\n")
    # A doubt reviewer judges the artifact on its own: no code map and no handoff, the author's account of the work.
    # The flag and role are tested first, so every other step's prompt is built exactly as before.
    doubter = (step.blocks_on_findings and step.role == "review" and extra.get("phase") is not None
               and doubt.is_doubt_reviewer(cfg, str(extra["phase"]), step, str(ctx.values.get("lane") or "")))
    if doubter:
        values["code_map"] = ""  # before the slot is dropped, so it goes whole
    preamble = PREAMBLE_SLOT.sub("", preamble)
    step_head = _template(cfg, STEP_HEAD)
    step_head = EMPTY_SLOT.sub(lambda m: "" if not values[m.group(1)] else m.group(0), step_head)
    body = _template(cfg, step.prompt or DEFAULT_TEMPLATE).strip()
    # The first prompt of a session carries the skill (`_inlined_skill`), a continuation not: its session has the text.
    inlined = None if step.continuation else _inlined_skill(cfg, step, ctx.repo, body)
    in_system = inlined is not None and bool(brief and brief.get("skill"))  # the session's brief holds the skill
    files_in_system = bool(brief and brief.get("files") and step.brief)  # ... or its `brief:` files
    skill_brief = None if in_system else inlined
    files = list(dict.fromkeys([*([] if files_in_system else step.brief), *step.task_brief]))
    briefs = [skill_brief] if skill_brief is not None else []
    briefs.extend((ctx.repo / name).read_text(encoding="utf-8") for name in files)
    pointer = (_brief_pointer(step.skill if in_system else None, files_in_system)
               if in_system or files_in_system else "")
    if "{brief}" not in body:
        if skill_brief is not None:
            body = f"{body}\n\n{SKILL_BLOCK}"
        elif in_system:
            body = f"{body}\n\n{SKILL_POINTER_BLOCK}"
    if not step.skill:  # no skill to name in a `## The `{skill}` skill` heading
        body = _drop_skill_section(body, keep=bool(briefs or pointer))
    elif step.continuation and not briefs and not pointer:  # the session holds the skill's text: no empty section
        body = _drop_skill_section(body)
    if step.skill and f"`{step.skill}` skill" not in body:
        if skill_brief is not None:
            line = f"Use the `{step.skill}` skill: its full text is in this prompt, so do not load it."
        elif in_system:
            line = f"Use the `{step.skill}` skill: its full text is in your system prompt, so do not load it."
        else:
            line = f"Use the `{step.skill}` skill."
        body = f"{line}\n\n{body}"
    extra_skills = overlays.skill_lines(cfg, ctx, str(rec.get("role") or step.role or DEFAULT_ROLE), body)
    if extra_skills:
        body = f"{body}\n\n{extra_skills}"
    if extra.get("gate_evidence") and "{gate_evidence}" not in body:  # a `gates_first` template that has no slot
        body = f"{body}\n\n{{gate_evidence}}"
    if extra.get("read_only_note") and "{read_only_note}" not in body:  # a template that has no slot for it
        body = f"{body}\n\n{{read_only_note}}"
    values["brief"] = "\n\n".join([pointer, *briefs] if pointer else briefs)
    values["skill"] = step.skill or ""
    values["base_ref"] = ctx.base_ref
    values["earlier_findings"] = ""
    values["gate_evidence"] = ""
    values["read_only_note"] = ""
    # The result file is read only when the template asks for it.
    values["verification"] = (verification.render_prompt(*verification.read_story(ctx.run))
                              if ctx.run is not None and "{verification}" in body else "")
    if ctx.run is not None and ctx.run.handoff_path().exists() and not doubter:
        body = f"{HANDOFF_LINE}\n\n{body}"
    if mode == "refine":
        refine = _template(cfg, cfg.phase_approval.get("refine_prompt") or DEFAULT_REFINE).strip()
        body = f"{refine}\n\nThe step as it was first given:\n\n{body}"
    if mode == "refine" or (ctx.run is not None and ctx.run.state.get("refine")):  # a step can first run in a refine round
        values.update(_refine_values(ctx, rec))
    values["in_brief"] = ""  # reserved (`config.PROMPT_PLACEHOLDERS`): always empty for now
    values.update({name: str(value) for name, value in extra.items()})
    parts = (step_head, body) if brief else (preamble, step_head, body, _template(cfg, RESULT))
    text = "\n\n".join(part.strip() for part in parts)
    agent = cfg.agents.get(rec.get("agent") or cfg.defaults.agent)
    if self_contained or (agent is not None and agent.skill_loading == "read_file"):
        carried = (skill_brief is not None or in_system) and step.skill
        if self_contained and step.skill and not carried:  # its `SKILL.md` as a `brief:` file carries it too
            own = PurePosixPath(cfg.project.skills_dir, step.skill, "SKILL.md")
            carried = any(PurePosixPath(name) == own for name in (*step.brief, *step.task_brief)) and step.skill
        text = _read_file_skills(text, cfg.project.skills_dir, frozenset({step.skill}) if carried else frozenset(),
                                 resolve=_skill_path(ctx.repo, cfg.project.skills_dir) if self_contained else None)
    return fmt(text, values) + "\n"


# --------------------------------------------------------------------------- the run brief


def build_brief(cfg: Config, run: Run, phase_name: str, step: Step, role: str, body: str,
                values: dict[str, str]) -> tuple[str, dict] | str:
    """The run brief of a fresh session of `step` (`brief.py`): `(text, pin)`, or why it has none.

    Everything taken from the repository is read in the main checkout at the run's fork commit (`state.base_sha`),
    through `gitops.read_blob`, never from a working tree: the rules (`project.agent_rules`), a pack override of
    `_preamble.md` or `_result.md` (`.agentflow/prompts/<name>`), the repository's skill and the step's `brief:` files.
    The engine's prompts and bundled skills come from the engine's files. `body` (the step's template) decides only
    whether the skill is carried (`_inlined_skill`), and `values` fill only `config.BRIEF_PLACEHOLDERS`, so two steps of
    a phase and role get the same brief. The pin is `{path, sha256, rules, skill, files, phase, role}`, plus
    `rules_fallback` (why) when the rules are not carried and Claude loads its own memory files."""
    try:
        return _assemble_brief(cfg, run, phase_name, step, role, body, values)
    except (OSError, ValueError, UnicodeError) as exc:
        return f"the brief could not be built: {exc}"[:BRIEF_REASON_MAX]


def _assemble_brief(cfg: Config, run: Run, phase_name: str, step: Step, role: str, body: str,
                 values: dict[str, str]) -> tuple[str, dict] | str:
    checkout = pack.checkout_root(run.repo)
    raw = run.state.get("base_sha")
    base = raw if isinstance(raw, str) and FULL_SHA.fullmatch(raw) else None
    listing: list[list[str] | None] = []

    def tree() -> list[str] | None:  # the proven listing at `base`, read at most once
        if not listing:
            listing.append(gitops.verified_tree_paths(checkout, base) if base else None)
        return listing[0]

    partials: dict[str, str] = {}
    for name in (PREAMBLE, RESULT):
        raw_text = _brief_partial(cfg, checkout, base, name)
        if raw_text is None:
            return "prompt override not at base_sha"
        partials[name], outside = brief_partial(name, raw_text)  # the same reading `validate` warns from
        if outside:
            return f"prompt override holds {{{outside[0]}}}"
    preamble, result = partials[PREAMBLE], partials[RESULT]
    fill = {name: str(values[name]) for name in BRIEF_PLACEHOLDERS if name in values}

    rules, carries = brief_mod.read_rules(checkout, base, cfg.project.agent_rules)
    docs: list[brief_mod.Doc] = []
    skill_in = files_in = False
    if step.skill and _inlined_skill(cfg, step, checkout, body) is not None:
        owned = _repo_skill(cfg, checkout, base, step.skill, tree)
        found = (brief_mod.read_skill(checkout, base, cfg.project.skills_dir, step.skill, owned)
                 if owned is not None else None)
        if found is not None:  # else the skill stays in the prompt, as without a brief
            docs.append(found)
            skill_in = True
    if step.brief:
        found_files = brief_mod.read_brief_files(checkout, base, step.brief)
        if found_files is not None:  # else they stay in the prompt
            docs.extend(found_files)
            files_in = True
    text = brief_mod.render(rules=rules, base_sha=base or "", preamble=fmt(preamble, fill), skill=docs,
                            result=fmt(result, fill))
    if len(text) > brief_mod.BRIEF_MAX_CHARS:
        return f"brief over {brief_mod.BRIEF_MAX_CHARS} chars"
    pin: dict = {"path": str(run.dir / BRIEF_DIR / brief_mod.file_name(phase_name, role, text)),
                 "sha256": brief_mod.digest(text), "rules": carries, "skill": skill_in, "files": files_in,
                 "phase": phase_name, "role": role}
    if not carries:
        pin["rules_fallback"] = _rules_fallback(cfg, base, tree)
    return text, pin


def _brief_partial(cfg: Config, checkout: Path, base: str | None, name: str) -> str | None:
    """`_preamble.md` or `_result.md` for a brief: the pack's override as the fork commit holds it (None when it holds
    none it can read), else the engine's own. A live pack prompt is never read."""
    own = Path(cfg.path).parent / "prompts" / name
    if pack.prompt_path(Path(cfg.path), name) == own:
        data = gitops.read_blob(checkout, base, f".agentflow/prompts/{name}", brief_mod.BRIEF_FILE_MAX_BYTES) \
            if base else None
        return None if data is None else data.decode("utf-8").replace("\r\n", "\n")
    return (pack.ENGINE_PROMPTS / name).read_text(encoding="utf-8")


def _repo_skill(cfg: Config, checkout: Path, base: str | None, name: str, tree) -> bool | None:
    """Whether skill `name` is the repository's own, from the fork commit's proven tree (None when that can't be
    proven: the skill then stays in the prompt). With no fork commit the brief carries no repository skill, so a
    skill the checkout has that the engine does not bundle reads as the repository's (and is left out)."""
    if base is None:
        found = pack.skill_file(checkout, cfg.project.skills_dir, name)
        return found is not None and not pack.is_bundled(found)
    listed = tree()
    if listed is None:
        return None
    return "/".join((*PurePosixPath(cfg.project.skills_dir).parts, name, "SKILL.md")) in listed


def _rules_fallback(cfg: Config, base: str | None, tree) -> str:
    """Why `read_rules` did not carry the rules, as far as the runner can tell without reading them again."""
    if base is None:
        return "no base_sha"
    if not cfg.project.agent_rules:
        return "project.agent_rules is empty"
    if tree() is None:
        return "the tree at base_sha could not be proven"
    return "a memory file the brief cannot carry, or a rules file that cannot be read at base_sha"


def _pin(value: object) -> dict | None:
    """A recorded pin, or None (no brief, or a session from before the brief)."""
    return value if isinstance(value, dict) else None


PIN_FIELDS: dict[str, type] = {"path": str, "sha256": str, "rules": bool, "skill": bool, "files": bool, "phase": str,
                               "role": str}  # what `build_brief` pins, every one of them
PIN_OPTIONAL: dict[str, type] = {"rules_fallback": str}
PIN_SHA = re.compile(r"[0-9a-f]{64}")


def _pin_ok(pin: dict) -> bool:
    """`pin` has exactly the fields `build_brief` gives, of their types (a flag is a bool, never a truthy stand-in) and
    a full SHA-256 digest. Anything else is malformed: the session's brief is replaced, never trusted."""
    fields = {**PIN_FIELDS, **PIN_OPTIONAL}
    if not set(PIN_FIELDS) <= set(pin) <= set(fields):
        return False
    return all(type(pin[key]) is fields[key] for key in pin) and bool(PIN_SHA.fullmatch(pin["sha256"]))


def _same_flags(a: dict, b: dict) -> bool:
    return all(type(a.get(flag)) is bool and a.get(flag) is b.get(flag) for flag in BRIEF_FLAGS)


# --------------------------------------------------------------------------- executor state


@dataclass
class _Turn:
    """The next agent call: its prompt and the session it resumes (None = fresh)."""

    prompt: str
    resume: str | None


@dataclass
class _Called:
    outcome: AgentOutcome | None  # None only with `brief_error`: no agent was called
    after: str
    refused: list[Violation] = dataclasses.field(default_factory=list)  # found before the tree could be read safely
    brief_error: str | None = None  # the session's pinned brief could not be given to the call (`_ensure_brief`)


@dataclass
class _StepRun:
    """One `run_agent_step` invocation's working state."""

    phase: Phase
    step: Step
    key: str
    mode: Mode
    role: str
    agent_name: str
    agent: Agent
    model: str
    effort: str
    extra: dict
    session: str | None
    agent_profile: str | None = None
    lead: str = ""  # put before every fresh prompt until the gates run again (a gate step's first fix)
    lead_fix: tuple[list[GateResult], str] | None = None  # what `lead` was built from: (failed gates, refine note)
    start_head: str = ""
    start_tree: str = ""
    resume_tree: str | None = None  # the tree when a human's approval resumed the session
    progress_mtime: float | None = None
    fixes: int = 0
    rollovers: int = 0
    reminded: bool = False
    holds: int = 0  # since the last result; these three reset once the agent can work again
    agent_failures: int = 0
    down_since: float | None = None
    reconcile_base: dict | None = None  # a reconcile step's `gitops.reconcile_baseline`, the checks compare against it
    brief: dict | None = None  # the session's pinned brief (`build_brief`); None: the session has none
    brief_text: str | None = None  # its text, kept in memory; None after a restart until `_ensure_brief` rebuilds it
    brief_skipped: str | None = None  # why the fresh session was refused one (`agent_started.brief_skipped`)
    brief_refused: str | None = None  # a fresh session's brief failed at the call: the next fresh one goes without
    brief_invalid: str | None = None  # why a resumed session's recorded pin can't be trusted (`_load_pin`)
    dry: bool = False  # `dry_agent_argv`: the brief is built but never written
    # A `read_only_roles` session (`_decide_read_only`): no edits, no skills listing, a shorter idle limit. A resumed
    # session keeps what it started with (`agent_started.read_only`, kept on the step row beside its session).
    read_only: bool = False
    # Any session of this step since its `step_started` was read-only (this process's, or a recorded `agent_started`):
    # the tree guard stays on, though a fresh session (a rollover, a config edit) re-decides `read_only` (`_guard_read_only`).
    read_only_seen: bool = False
    # This step's turn cap, over `agents.<name>.max_turns` on every call of it; None: the agent's own (a later doubt
    # round's review gets half, `doubt.delta_turn_cap`).
    max_turns: int | None = None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None


def _count(events: list[dict], etype: str, key: str) -> int:
    return sum(1 for e in events if e["type"] == etype and e.get("key") == key)


def _answers(events: list[dict], key: str) -> dict[str, str]:
    """The human's answers for `key` given since its last agent call, if any."""
    found: dict[str, str] = {}
    for event in events:
        if event["type"] == "agent_started" and event.get("step") == key:
            found = {}
        elif event["type"] == "hitl_answered" and event.get("key") == key:
            found = dict(event.get("answers") or {})
    return found


REOPENING = ("hitl_approved", "step_reopened")
DENIED_SHOWN = 15  # commands listed in a blocked inbox
DENIED_TEXT_MAX = 200
READ_ONLY_DENIED = "This step runs read-only (`agents.claude.read_only_roles`): these denials are by design."
READ_ONLY_REVERT = "Revert these before the run continues: a later step commits the whole work tree."
READ_ONLY_EDITS_SHOWN = DENIED_SHOWN  # `read_only_edit` paths named in a stop; the event keeps them all


def _approved(events: list[dict], key: str) -> dict | None:
    """Since the last agent call on `key`: the latest approval of a runner stop on it, or the runner
    reopening it (`step_reopened`, with the note for the session), if any."""
    found = None
    for event in events:
        if event["type"] == "agent_started" and event.get("step") == key:
            found = None
        elif event["type"] in REOPENING and event.get("key") == key:
            found = event
    return found


def _after_approval(events: list[dict], key: str) -> list[dict]:
    """The events after the step was last approved or reopened: limits like fix attempts restart there."""
    for index in range(len(events) - 1, -1, -1):
        if events[index]["type"] in REOPENING and events[index].get("key") == key:
            return events[index + 1 :]
    return events


def _violation_text(violations: list[Violation]) -> str:
    """The violations for a stop's summary. The `read_only_edit` paths are listed once, where the first one is, each
    quoted (`json.dumps`: a newline or a control character in a name stays on its line) and at most
    `READ_ONLY_EDITS_SHOWN` of them, then `and <k> more`; the `guard_tripped` event keeps every path."""
    edits = [v.detail for v in violations if v.kind == "read_only_edit"]
    parts: list[str] = []
    listed_edits = False
    for v in violations:
        if v.kind != "read_only_edit":
            parts.append(f"{v.kind}: {v.detail}")
        elif not listed_edits:
            listed_edits = True
            listed = ", ".join(json.dumps(path) for path in edits[:READ_ONLY_EDITS_SHOWN])
            more = f" and {len(edits) - READ_ONLY_EDITS_SHOWN} more" if len(edits) > READ_ONLY_EDITS_SHOWN else ""
            parts.append(f"read_only_edit: {listed}{more}")
    return "; ".join(parts)


# --------------------------------------------------------------------------- executor


class Executor(BugEvalLane, StepControl, FeatureLane, SyncLane, CILane, SizingLane, RouteLane):
    """Runs a run's steps: `run_steps`/`run_step` (control flow, in `flow_control`), plan tasks and
    `finish` (in `feature_lane`), test expectations and fix-looping gate steps (in `bug_eval`) and the
    agent step lifecycle here. `dry_run` prints each agent's command line and calls nothing."""

    def __init__(
        self,
        cfg: Config,
        run: Run,
        agents: dict[str, Agent],
        ctx: Ctx,
        overrides: dict[str, str],
        dry_run: bool = False,
        out: TextIO = sys.stdout,
        briefs: bool = True,
    ) -> None:
        self.cfg = cfg
        self.run = run
        self.agents = agents
        if ctx.run is None:
            ctx = dataclasses.replace(ctx, run=run, values={**ctx.values, "run": run.id})
        self.ctx = ctx
        self.overrides = overrides
        self.dry_run = dry_run
        self.out = out
        self.briefs = briefs  # the retro's session gets none
        self._brief_texts: dict[str, tuple[str, dict]] = {}  # by digest: what this executor wrote, for shared sessions

    @property
    def worktree(self) -> Path:
        return Path(self.run.state.get("worktree") or self.ctx.repo)

    # -- the lifecycle

    def run_agent_step(
        self,
        phase: Phase,
        step: Step,
        key: str,
        extra: dict[str, str] | None = None,
        mode: Mode = "first",
        role: str | None = None,
        share_session_of: str | None = None,
        fix_first: list[GateResult] | None = None,
        size: bool = True,
        turn_cap: int | None = None,
    ) -> Outcome:
        """Run `step` as `key` until it is done ("done"), or a human is needed ("stopped").

        A step already done is not run again; a step skipped by its condition, or any step in a
        dry run, is "skipped". A step whose record is `running` or `waiting_human` (the runner was
        stopped, or a human answered) resumes its recorded session at its recorded effort.
        `share_session_of` names a step whose session a new step continues (at that step's effort),
        as the tasks of a small plan do. A step parked by a question that later
        tasks need not wait for returns "stopped" with its record `parked`.
        `fix_first` (gates that already failed, as for a gate step) starts a step that is not resuming
        in its fix loop: the first call is fix attempt 1, with the gate output.
        `turn_cap` replaces the agent's `max_turns` for every call of the step (a later doubt round's review).
        With `size` and `effort.sizing`, a step that has not started is sized first (`SizingLane.size_step`), unless
        it continues a shared session, the run is over budget, or it is a dry run. Whether sized or not, a sizer of
        the step left open by a runner that died is checked first (`SizingLane.check_open_sizers`). A review that
        sends the step back to its session (`step_reopened` for `review`, or a refine round) raises its effort once
        (`SizingLane.escalate_effort`), as its first fix attempt after a failed gate does in `_loop`. A judgement step
        never runs below `medium` (`sizing.floor`), on any path.
        """
        extra = self._step_extra(key, extra)
        self.run.refresh()
        state = self.run.state
        rec = state["steps"].get(key) or {}
        status = rec.get("status")
        if status == "skipped":
            return "skipped"
        if status in ("done", "unchanged"):
            return "done"
        if state["waiting"] is not None or halted(state["status"]):
            return "stopped"

        agent_name = phase.agent or self.cfg.defaults.agent
        agent = self.agents.get(agent_name)
        if agent is None:
            raise ValueError(f"agent {agent_name!r} is not enabled; phase {phase.name!r} needs it")
        role = role or step.role or DEFAULT_ROLE
        since = self._since_step_started(key)
        started = since[0] if since else None
        # A stop for a sizer of this step that changed the repository: the step is sized again and starts fresh, even
        # when an earlier attempt (since retried) had started a session.
        tripped = not self.dry_run and self._sizer_trip(key) is not None
        resuming = status in RESUMABLE and started is not None and not tripped
        session = rec.get("session_id")
        reuses_session = bool(session) and (resuming or mode == "refine")
        if reuses_session and started is not None and started.get("model"):
            model = started["model"]
            source = started.get("model_source") or "state"
        else:
            model, source = resolve_model(self.cfg, phase, step, role, self.overrides)
        sr = _StepRun(
            phase=phase, step=step, key=key, mode=mode, role=role, agent_name=agent_name, agent=agent,
            model=model, effort=rec.get("effort") or self.cfg.effort.default, extra=extra, session=session,
            agent_profile=overlays.agent_profile(self.cfg, step), max_turns=turn_cap,
        )
        judgement = sizing.is_judgement(phase.name, role)
        sr.effort = sizing.floor(sr.effort, judgement, self.cfg.effort.allowed)
        # A sizer of this step changed the repository: nothing runs until it is back as the sizer found it.
        resized = False
        if tripped:
            left = self.sizer_reset_needed(key)
            if left:
                return self._fail(sr, [Violation("sizer_edit", "reset " + ", ".join(left) + " first")])
            resized = True  # cleared: the step is sized again, whatever its row says
        # A sizer of this step left open (its runner died): compared before anything runs, sized or not.
        if not self.dry_run and not self.check_open_sizers(key):
            return "stopped"

        shared = (state["steps"].get(share_session_of) or {}) if share_session_of else {}
        takes_shared = not session and status in UNSTARTED_STEP and bool(shared.get("session_id"))
        if (size and self.cfg.effort.sizing and not self.dry_run and mode == "first" and not resuming
                and (status in UNSTARTED_STEP or resized) and (not rec.get("sized") or resized)
                and not takes_shared and not sizing.is_sizer_key(key) and self._under_budget()):
            level = self.size_step(sr, [r.name for r in fix_first or []])
            if level is None:
                return "stopped"
            sr.effort = sizing.floor(level, judgement, self.cfg.effort.allowed)
        reopened = _approved(since, key) if resuming else None
        if not self.dry_run and ((reopened is not None and reopened["type"] == "step_reopened"
                                  and reopened.get("reason") == "review") or (mode == "refine" and session)):
            self.escalate_effort(sr, "review")  # a review sent the step back to its session
            sr.effort = sizing.floor(sr.effort, judgement, self.cfg.effort.allowed)
        # A resumed session keeps the brief it started with (its pin in state), whatever the config says now.
        # ... and the read-only setting it started with, whatever `read_only_roles` says now.
        if resuming and session:
            self._load_pin(sr, rec, session)
            self._load_read_only(sr, rec, session)
            turn = _Turn(self._continue_prompt(sr, _answers(since, key), _approved(since, key)), session)
        elif mode == "refine" and session:
            self._load_pin(sr, rec, session)
            self._load_read_only(sr, rec, session)
            turn = _Turn(self._refine_prompt(sr), session)
        elif takes_shared:
            sr.session = shared["session_id"]
            sr.effort = sizing.floor(shared.get("effort") or sr.effort, judgement, self.cfg.effort.allowed)
            self._load_pin(sr, shared, sr.session)
            self._load_read_only(sr, shared, sr.session)
            turn = _Turn(self._prompt(sr, resume=sr.session), sr.session)
        else:
            turn = _Turn(self._prompt(sr), None)
        fixing = bool(fix_first) and not resuming
        if fixing:  # a gate step's session starts from the gate output (and a refine round's note)
            refine = current_refine(self.run, phase.name)
            note = f"\n\n{refine_note_block(refine)}" if refine is not None else ""
            sr.lead_fix = (list(fix_first or []), note)
            sr.lead = f"{self._fix_lead(sr, fix_first or [], 1, excerpt=turn.resume is not None)}{note}"
            turn = dataclasses.replace(turn, prompt=f"{sr.lead}\n\n{turn.prompt}")

        if self.dry_run:
            call = self._agent_call(sr, turn, attempt=rec.get("attempts", 0) + 1)
            print(shlex.join(agent.argv(call)), file=self.out)
            return "skipped"

        # A reconcile step starts only on a tree that holds nothing but its artifact: a stray file left from an earlier
        # stop would otherwise ride along (and the step must not take it for its own work). Checked before the
        # snapshots below, which run `git add`; a resumed step is checked once its earlier agents are gone.
        if step.reconciles and not (resuming and started is not None):
            outside = self._reconcile_start(sr, fresh=True)
            if outside:
                return self._fail(sr, outside)
        if resuming and started is not None:
            committed = [e for e in since if e["type"] == "step_committed" and e.get("key") == key]
            if committed:
                last = committed[-1]
                self.run.emit("step_finished", key=key, status="done", summary=last.get("summary", ""), sha=last["sha"])
                return "done"
            wait_for_agents(self.run, self.cfg, key, self._timeout_s())
            if step.reconciles:  # after the wait: an agent left running could still change the tree until then
                outside = self._reconcile_start(sr, fresh=False)
                if outside:
                    return self._fail(sr, outside)
            baseline = self._baseline(key, since, started)
            if isinstance(baseline, list):
                return self._fail(sr, baseline)
            sr.start_head, sr.start_tree = baseline
            # a session of this step since that baseline ran read-only (one a dead runner left, too): the guard stays on
            sr.read_only_seen = sr.read_only_seen or any(
                e["type"] == "agent_started" and (e.get("step") or e.get("key")) == key and e.get("read_only") is True
                for e in since)
            if (_approved(since, key) or {}).get("type") == "hitl_approved":
                sr.resume_tree = snapshot(self.worktree)
            fresh = _after_approval(since, key)
            sr.fixes = _count(fresh, "fix_attempt", key)
            sr.rollovers = _count(fresh, "rolled_over", key)
        else:
            sr.start_head = head(self.worktree)
            sr.start_tree = snapshot(self.worktree)
            if "failed" in (status, rec.get("retry_of")):  # a restart must not adopt the earlier violation
                known = runner_head(self.run)
                if known and sr.start_head != known:  # e.g. the commit an agent made: never the new baseline
                    return self._fail(sr, [Violation("agent_commit", f"HEAD is {sr.start_head[:12]}, not the "
                                                     f"{known[:12]} the runner last recorded: reset it first")])
                dirty = self._uncommitted_forbidden(sr.start_tree)
                if dirty:
                    return self._fail(sr, [Violation("forbidden_path", p) for p in dirty])
                left = self._read_only_edits_left(since, sr.start_tree)
                if left:  # a reviewer's edits still in the tree: a restart would adopt them, then a commit take them
                    return self._fail(sr, [Violation("read_only_edit", p) for p in left])
            refine = state.get("refine") or {}
            round_ = refine.get("round") if mode == "refine" and refine.get("phase") == phase.name else None
            self.run.emit(
                "step_started", key=key, phase=phase.name, agent=agent_name, model=model, model_source=source,
                effort=sr.effort, mode=mode, round=round_, start_head=sr.start_head, start_tree=sr.start_tree,
                task=self._task_payload(key),
            )
            if fixing:
                sr.fixes = 1
                self.run.emit("fix_attempt", key=key, attempt=1, gates=[r.name for r in fix_first or []])
        sr.progress_mtime = _mtime(self.run.progress_path())
        return self._loop(sr, turn)

    def dry_agent_argv(self, phase: Phase, step: Step, key: str, extra: dict | None = None,
                       role: str | None = None) -> list[str]:
        """The command line a fresh first call of `step` would get, built without touching the run."""
        agent_name = phase.agent or self.cfg.defaults.agent
        agent = self.agents.get(agent_name)
        if agent is None:
            raise ValueError(f"agent {agent_name!r} is not enabled; phase {phase.name!r} needs it")
        role = role or step.role or DEFAULT_ROLE
        model, _source = resolve_model(self.cfg, phase, step, role, self.overrides)
        effort = sizing.floor(self.cfg.effort.default, sizing.is_judgement(phase.name, role), self.cfg.effort.allowed)
        sr = _StepRun(
            phase=phase, step=step, key=key, mode="first", role=role, agent_name=agent_name, agent=agent,
            model=model, effort=effort, extra=self._step_extra(key, extra), session=None,
            agent_profile=overlays.agent_profile(self.cfg, step), dry=True,
        )
        turn = _Turn(self._prompt(sr), None)
        return agent.argv(self._agent_call(sr, turn, attempt=1))

    def _under_budget(self) -> bool:
        """The run's spend is under its hard budget (the check `_loop` stops on)."""
        budget = self.run.state.get("budget_usd") or self.cfg.defaults.run_budget_usd
        return not (budget and float(self.run.state.get("spend_usd") or 0.0) >= budget)

    def _loop(self, sr: _StepRun, turn: _Turn) -> Outcome:
        while True:
            budget = self.run.state.get("budget_usd") or self.cfg.defaults.run_budget_usd
            spend = float(self.run.state.get("spend_usd") or 0.0)
            soft = self.run.state.get("soft_budget_usd")
            if soft and spend >= soft and not self._soft_noted(soft):
                self.run.emit("budget_soft_reached", spend_usd=spend, soft_usd=soft, hard_usd=budget)
                notify_run(self.cfg, self.run, f"spent ${spend:.2f}: past the soft budget ${soft:.2f}; "
                                               f"it goes on and stops at ${budget:.2f}")
            if budget and spend >= budget:
                which = "hard budget" if soft is not None else "budget"  # a run from before the soft budget has one
                return self._stop(sr, "budget_exceeded", f"The run has spent ${spend:.2f} of its ${budget:.2f} {which}.")

            called = self._call(sr, turn)
            if called.brief_error is not None:  # no agent ran: the session can't have its brief, so a fresh one starts
                turn = self._replace_brief(sr, turn, called.brief_error)
                continue
            if self.run.refresh()["status"] in TERMINAL:
                return "stopped"  # cancelled while the agent ran: nothing more is called or recorded
            outcome = called.outcome  # set whenever `brief_error` is not
            if called.refused:  # the git state changed: nothing that reads the tree has run, and nothing may
                return self._fail(sr, called.refused)
            violations = check_step(self.cfg, self.worktree, sr.start_head, sr.start_tree, called.after,
                                    outcome.tool_calls, sr.step.forbid, read_only=self._guard_read_only(sr))
            if sr.step.reconciles:
                violations = [*violations, *self._outside_output(sr)]
            if violations:
                return self._fail(sr, violations)
            if outcome.interruption is not None and outcome.interruption.kind == "cli_flags":
                return self._stop(sr, "blocked", self._refused_flag_summary(sr, outcome.interruption.detail))
            if outcome.resume_failed:
                if sr.mode == "refine" and turn.resume:
                    turn = self._fresh_refine(sr, turn.resume)
                    continue
                return self._stop(sr, "blocked", f"Could not resume agent session {turn.resume}.")
            if outcome.timed_out:
                limit = self.cfg.defaults.step_timeout_min
                return self._stop(sr, "step_timeout", f"The agent call ran past the step timeout ({limit} min).")
            if outcome.stalled:
                quiet = round((outcome.idle_limit_s or 0) / 60, 1)
                self.run.emit("watchdog_fired", key=sr.key, idle_min=quiet)
                return self._stop(sr, "step_timeout",
                                  f"The agent made no progress for {quiet} min, so the runner ended the session.")

            result, error = self._read_result(sr)
            if result is None:
                if outcome.rolled_over:
                    next_turn = self._rollover(sr, outcome)
                elif outcome.interruption is not None:
                    next_turn = self._hold(sr, turn, outcome.interruption)
                elif not sr.reminded:
                    sr.reminded = True
                    self.run.emit("result_reminded", key=sr.key, error=error)
                    next_turn = _Turn(self._reminder_prompt(sr, error), sr.session)
                else:
                    self._stop(sr, "blocked", f"No valid result.json after a reminder: {error}")
                    next_turn = None
                if next_turn is None:
                    return "stopped"
                turn = next_turn
                continue
            sr.reminded = False
            sr.holds, sr.agent_failures, sr.down_since = 0, 0, None

            if result.status == "effort_change":
                turn = self._effort_change(sr, called, result)
                continue
            if result.status == "needs_human" and not writes_questions(sr.step):  # a grill's questions are its output
                questions = [asdict(q) for q in result.questions]
                # A set the runner answers itself is never parked: it is raised quietly, `Resolver.take_over` (the one
                # place the runner answers) takes the recommended options, and the step resumes after any `record_step`.
                if (result.can_continue_without and self.may_park(sr.key)
                        and not runner_settles(self.cfg, self.run, "needs_human", sr.key, questions)):
                    held = changed_paths(self.worktree, sr.start_tree, called.after)
                    park_hitl(self.cfg, self.run, "needs_human", sr.key, result.summary, result.questions, held)
                    return "stopped"
                raise_hitl(self.cfg, self.run, "needs_human", sr.key, result.summary, result.questions)
                return "stopped"
            if result.status == "blocked":
                kind = "blocked_repeat" if called.after == sr.resume_tree else "blocked"
                return self._stop(sr, kind, result.summary or "The agent reported the step as blocked.",
                                  extra=self._denied_report(sr.key, read_only=sr.read_only))
            if result.status == "unchanged":
                edited = changed_paths(self.worktree, sr.start_tree, called.after)
                if edited:  # never leave edits uncommitted and ungated under an `unchanged` label
                    shown = ", ".join(edited[:5]) + (f" and {len(edited) - 5} more" if len(edited) > 5 else "")
                    return self._stop(sr, "blocked", f"The agent answered `unchanged` but changed {shown}. "
                                      "Approve to resume the step: it must revert those edits or finish as `done`.")
                self.run.emit("step_unchanged", key=sr.key, reason=result.summary)
                return "done"

            failed = self._failed_gates(sr)
            sr.lead, sr.lead_fix = "", None  # the gate output it carried is stale now; fix prompts carry the new output
            expected = self.check_expect(sr, result, gates_failed=bool(failed))
            if expected == "stopped":
                return "stopped"
            failed = [*failed, *expected]
            if failed:
                if (sr.step.fix_loop or sr.step.checks) and sr.fixes < self.cfg.defaults.fix_attempts:
                    sr.fixes += 1
                    self.escalate_effort(sr, "gate")  # once per step; the fix resumes the session one level up
                    self.run.emit("fix_attempt", key=sr.key, attempt=sr.fixes, gates=[r.name for r in failed])
                    turn = _Turn(self._fix_prompt(sr, failed), sr.session)
                    continue
                names = ", ".join(r.name for r in failed)
                return self._stop(
                    sr, exhausted_kind(sr.step),
                    f"Gates still failing after {sr.fixes} fix attempt(s): {names}.", extra=self._gate_report(failed),
                )
            return self._finish(sr, result)

    # -- one call

    def _timeout_s(self) -> int:
        return max(1, round(self.cfg.defaults.step_timeout_min * 60))

    def _idle_s(self, read_only: bool = False) -> tuple[float | None, float | None]:
        """The watchdog's two limits (`agents.idle_limit`): `watchdog.no_progress_min`, and while a tool call is open
        at least `defaults.gate_timeout_min`, since a gate-length command writes nothing until it returns. 0: off.
        A `read_only` session with no tool call open ends sooner, after `watchdog.read_only_idle_min` (0: no sooner);
        while one is open it keeps the same limit, since a reviewer may run a long command."""
        minutes = self.cfg.watchdog.no_progress_min
        if not minutes > 0:  # 0, and a value `validate` refuses, never arm a watchdog that fires at once
            return None, None
        idle = minutes * 60
        tool = max(idle, self.cfg.defaults.gate_timeout_min * 60)
        quiet = self.cfg.watchdog.read_only_idle_min
        if read_only and quiet > 0:
            return min(minutes, quiet) * 60, tool
        return idle, tool

    def _agent_call(self, sr: _StepRun, turn: _Turn, attempt: int) -> AgentCall:
        idle_s, tool_idle_s = self._idle_s(sr.read_only)
        return AgentCall(
            prompt=turn.prompt,
            model=sr.model,
            effort=sr.effort,
            cwd=self.worktree,
            run_id=self.run.id,
            step_key=sr.key,
            result_path=self.run.agent_result_path(sr.key),
            transcript_path=self.run.transcript_path(sr.key, attempt),
            # The workspace only: never the runs root (events, state, inbox). A read-only call may write the `out/`
            # folder of every add-dir (`agents.read_only_allow`), so nothing else may ever join this list.
            add_dirs=[self.run.agent_dir],
            runs_root=self.run.dir.parent,  # denied to the agent's edits, wherever the pack puts the state dir
            timeout_s=self._timeout_s(),
            resume_session=turn.resume,
            agent_profile=sr.agent_profile,
            context_window=self.context_window_for(sr.key, sr.model),
            env=self.cfg.defaults.env,
            env_base=run_env.base(self.cfg),
            forbidden_paths=[*self.cfg.guardrails.forbidden_paths, *sr.step.forbid],
            idle_s=idle_s,
            tool_idle_s=tool_idle_s,
            usage_windows=dict(self.run.state.get("usage_window") or {}),
            # Every call of a session with a brief passes it, the resumes too (fix, reminder, continue, effort change).
            system_prompt_file=Path(str(sr.brief.get("path"))) if sr.brief is not None else None,
            carries_rules=bool(sr.brief is not None and sr.brief.get("rules") is True),
            # Every call of a session keeps the setting the session started with (`_decide_read_only`).
            read_only_role=sr.read_only,
            self_contained=self._self_contained(sr),
            max_turns=sr.max_turns,
        )

    def context_window_for(self, key: str, model: str) -> int | None:
        """The window the run has seen for this step's model: by the stream's model id when the
        step's record names one, else by the configured alias (`sonnet`); None when unseen."""
        windows = self.run.state["context_windows"]
        recorded = (self.run.state["steps"].get(key) or {}).get("model")
        for name in (recorded, model):
            if name and windows.get(name):
                return int(windows[name])
        return None

    def _call(self, sr: _StepRun, turn: _Turn) -> _Called:
        missing = self._ensure_brief(sr)  # right before the call: the file the CLI reads is the pinned text
        if missing is not None:
            return _Called(None, "", brief_error=missing)
        workspace.prepare(self.run)  # fresh copies of the inputs; whatever the agent left there is replaced
        workspace.clear_result(self.run, sr.key)  # a stale result must never count
        attempt = (self.run.state["steps"].get(sr.key) or {}).get("attempts", 0) + 1
        call = self._agent_call(sr, turn, attempt)
        seen: dict[str, object] = {"model": None, "pid": None}

        def on_event(name: str, payload: dict) -> None:
            data = dict(payload)
            if name == "agent_started":
                seen["pid"] = data.get("pid")
                data.update(sizing=False, phase=sr.phase.name, role=sr.role)  # a sizer's own call says True
                data["brief"] = sr.brief  # on every call, so a shared session's later task carries the pin on
                data["read_only"] = sr.read_only  # likewise: a resume takes the session's own setting
                if turn.resume is None and sr.brief_skipped:
                    data["brief_skipped"] = sr.brief_skipped
            elif name == "agent_session":
                seen["model"] = data.get("model")
                data.update(key=sr.key, pid=seen["pid"])
            elif name == "agent_exited":
                data["key"] = sr.key
            self.run.emit(name, **data)

        if turn.resume is None:
            self._record_lessons(sr, turn)
        outcome = sr.agent.run(call, on_event)
        self._note_ignored_settings(sr, outcome)
        sr.session = outcome.session_id or turn.resume or sr.session
        window = outcome.context_window
        if window:  # cached under the stream's model id and the alias, so the next call finds it either way
            for name in dict.fromkeys((str(seen["model"] or sr.model), sr.model)):
                if self.run.state["context_windows"].get(name) != window:
                    self.run.emit("context_window", model=name, tokens=window)
        footer = footer_next(outcome.final_text)
        if footer:
            self.run.emit("skill_footer", key=sr.key, phase=sr.phase.name, next=footer)
        for denial in outcome.permission_denials:
            by_design = {"read_only": True} if sr.read_only else {}  # `signals` leaves these out: no rule to add
            self.run.emit(
                "permission_denied", key=sr.key,
                tool=denial.get("tool_name") or denial.get("tool"),
                input=denial.get("tool_input", denial.get("input")), **by_design,
            )
        if sr.step.reconciles and sr.reconcile_base is not None:  # before the snapshot: it runs `git add`
            planted = git_state_changes(self.worktree, sr.reconcile_base)
            if planted:
                return _Called(outcome, sr.start_tree, planted)
        return _Called(outcome, snapshot(self.worktree))

    def _note_ignored_settings(self, sr: _StepRun, outcome: AgentOutcome) -> None:
        """Say once per run (`cli_setting_ignored`) when the CLI did not honour `cache_ttl` or `lean`. Only a Claude
        run call is checked; the run goes on."""
        spec = self.cfg.agents.get(sr.agent_name)
        if sr.agent_name != "claude" or spec is None:
            return
        for setting in ignored_settings(spec, outcome.usage):
            if setting not in self.run.state["cli_settings_ignored"]:
                self.run.emit("cli_setting_ignored", key=sr.key, setting=setting, agent=sr.agent_name)

    def _record_lessons(self, sr: _StepRun, turn: _Turn) -> None:
        """A fresh session's prompt carries the lessons: record which ones (and their digest), and which a size
        limit dropped. Only a prompt that really holds the selected text counts, so the record is what the agent saw."""
        picked = _lessons(self.cfg, self.ctx.repo, sr.key.split("/")[0], date.today())
        if picked.dropped:
            self.run.emit("memory_truncated", key=sr.key, tier="lessons", dropped=list(picked.dropped))
        if picked.ids and picked.text.rstrip("\n") in turn.prompt:
            self.run.emit("lessons_injected", key=sr.key, ids=list(picked.ids), digest=picked.digest)

    def _read_result(self, sr: _StepRun) -> tuple[StepResult | None, str]:
        """The agent's result, read from the workspace into the run directory's copy and checked there."""
        why = workspace.take_result(self.run, sr.key)
        if why:
            return None, why
        try:
            return read_result(self.run.result_path(sr.key), refine=sr.mode == "refine"), ""
        except ResultError as exc:
            return None, str(exc)

    # -- outcomes

    def _effort_change(self, sr: _StepRun, called: _Called, result: StepResult) -> _Turn:
        """Always refuse: the runner sizes each step before it starts, so no prompt offers a change."""
        requested = str(result.effort)
        why = "the runner sizes each step before it starts"
        self.run.emit("effort_refused", key=sr.key, requested=requested, reason=why, agent_reason=result.reason)
        note = f"Your effort change to `{requested}` was refused: {why}. Carry on with the step at `{sr.effort}`."
        return _Turn(f"{note}\n\n{self._result_partial(sr)}", sr.session)

    def _hold(self, sr: _StepRun, turn: _Turn, why: Interruption) -> _Turn | None:
        """The agent cannot work for now: wait, then return the next turn, or None when the run stopped.

        A usage limit waits for its reset, with no cap, since waiting is free. An outage backs off until
        `recovery.give_up_h`, and a call that reached the model and died counts against
        `recovery.failed_attempts`, since those cost money. A call no model answered is sent again as it was."""
        now = time.time()
        if why.kind == "usage_limit":
            reason = "usage limit"
            until = max(float(why.until or 0.0) + HOLD_SLACK_S, now + BACKOFF_START_S)
        else:
            policy = self.cfg.recovery
            sr.agent_failures += why.reached_model
            sr.down_since = sr.down_since or now
            if sr.agent_failures >= policy.failed_attempts or now - sr.down_since >= policy.give_up_h * 3600:
                self._stop(sr, "agent_unavailable",
                           f"The agent could not finish a call: {sr.agent_failures} died after reaching the model, "
                           f"{(now - sr.down_since) / 60:.0f} min since the first failure. Check the transcript.")
                return None
            reason = "agent died" if why.reached_model else "outage"
            until = now + min(BACKOFF_START_S * 2 ** sr.holds, BACKOFF_MAX_S)
        self.run.emit("run_held", key=sr.key, reason=reason, limit=why.limit,
                      until=datetime.fromtimestamp(until, UTC).isoformat(timespec="seconds"))
        if not sr.holds:
            notify_run(self.cfg, self.run, f"{sr.key} is held ({reason}); it carries on by itself.")
        sr.holds += 1
        waited = self._wait_until(until)
        self.run.emit("run_hold_ended", key=sr.key)
        if not waited:
            return None
        return _Turn(self._continue_prompt(sr, {}), sr.session) if why.reached_model else turn

    def _wait_until(self, until: float) -> bool:
        """Sleep until `until` (epoch seconds); False as soon as a human paused or ended the run."""
        while time.time() < until:
            if halted(self.run.refresh()["status"]):
                return False
            time.sleep(min(HOLD_POLL_S, max(0.0, until - time.time())))
        return not halted(self.run.refresh()["status"])

    def _rollover(self, sr: _StepRun, outcome: AgentOutcome) -> _Turn | None:
        """The session crossed `rollover_pct`: continue in a fresh one, or stop past `max_rollovers`."""
        self.run.emit("rolled_over", key=sr.key, session_id=outcome.session_id, context_pct=outcome.context_pct)
        sr.rollovers += 1
        limit = self.cfg.context.max_rollovers
        if sr.rollovers > limit:
            self._stop(sr, "max_rollovers",
                       f"The step ran out of context {sr.rollovers} times (limit {limit}); split it into smaller steps.")
            return None
        self._fresh_session(sr)
        values = self._values(sr)
        lead = (
            f"This is a fresh session: the previous one for this step ran out of context. "
            f"Continue the step from `{values['progress']}` and `{values['handoff']}`: read them first, "
            "check the work tree, and do not redo finished work."
        )
        return _Turn(f"{lead}\n\n{self._prompt(sr)}", None)

    def _fresh_refine(self, sr: _StepRun, gone: str) -> _Turn:
        """A refine round could not resume the step's session (expired, rolled over): a fresh session gets
        the same prompt plus `progress.md` and `handoff.md`, at the same effort."""
        self.run.emit("resume_failed", key=sr.key, session_id=gone, effort=sr.effort)
        self._fresh_session(sr)
        values = self._values(sr)
        lead = (
            f"This is a fresh session: the runner could not resume this step's earlier session. "
            f"Read `{values['progress']}` and `{values['handoff']}` first: they say what the step did before. "
            "Then check the work tree and refine the step's existing output; do not start over."
        )
        return _Turn(f"{lead}\n\n{self._prompt(sr)}", None)

    def _failed_gates(self, sr: _StepRun) -> list[GateResult]:
        """The step's gates, then its doc checks (`checks:`, on the diff since the step started); the failed ones.

        A gate that already passed in this run on the tree the step has now, with the same command, is not run
        again (`run_gates_reusing`): that covers a `gates_first` step whose session changed nothing, and a step that
        changed nothing after another step gated the same tree. In a fix attempt, the gates the last attempt named
        run first; while one of them still fails, the rest wait for the next attempt (they still run, on the final
        tree, before the step can finish)."""
        results: list[GateResult] = []
        tree = snapshot(self.worktree)
        if sr.step.gates:
            names = [g.name for g in resolve_gates(self.cfg, sr.step.gates)]
            first = [n for n in dict.fromkeys(self._last_fix_gates(sr)) if n in names] if sr.fixes else []
            log_dir = self.run.gate_log_dir(sr.key)
            tiered = self._gate_tier(sr, tree)
            if first:
                results = run_gates_reusing(self.cfg, self.run, sr.key, first, self.worktree, log_dir, tree, **tiered)
                if gates_ok(results):
                    rest = [n for n in names if n not in first]
                    if rest:
                        results += run_gates_reusing(self.cfg, self.run, sr.key, rest, self.worktree, log_dir, tree,
                                                     **tiered)
                else:
                    self.run.emit("gates_deferred", key=sr.key, tree=tree, gates=[n for n in names if n not in first])
            else:
                results = run_gates_reusing(self.cfg, self.run, sr.key, names, self.worktree, log_dir, tree, **tiered)
        if sr.step.checks:
            docs = self.cfg.project.docs
            results += run_doc_checks(sr.step.checks, self.worktree, sr.start_head, docs["idea"], docs["decisions"])
        record_results(self.run, sr.key, results, tree, with_tail=True)
        return [] if gates_ok(results) else [r for r in results if not (r.passed or r.skipped)]

    def _gate_tier(self, sr: _StepRun, tree: str) -> dict:
        """`run_gates_reusing`'s `tier` and `changed` for the step: at `gate_tier: affected`, the paths that differ
        between the commit the step started on and `tree`. A step with no start commit runs the full tier."""
        if sr.step.gate_tier != "affected" or not sr.start_head:
            return {"tier": "full", "changed": ()}
        return {"tier": "affected", "changed": changed_paths(self.worktree, sr.start_head, tree)}

    def _last_fix_gates(self, sr: _StepRun) -> list[str]:
        """The gates the step's latest `fix_attempt` named: the ones its fix was for."""
        attempts = [e for e in self.run.events.read() if e["type"] == "fix_attempt" and e.get("key") == sr.key]
        return [str(name) for name in (attempts[-1].get("gates") or [])] if attempts else []

    def _finish(self, sr: _StepRun, result: StepResult) -> Outcome:
        if sr.step.reconciles:  # checked again after the gates, before any snapshot or commit
            outside = self._outside_output(sr)
            if outside:
                return self._fail(sr, outside)
        if _mtime(self.run.progress_path()) in (None, sr.progress_mtime):
            self.run.emit("progress_not_updated", key=sr.key)
        sha = None
        tree = snapshot(self.worktree) if sr.step.gates else None  # what the gates passed on, for later checks
        if sr.step.commit is True:
            subject = fmt(sr.step.message or f"Step {sr.key}", self._values(sr))
            refine = current_refine(self.run, sr.phase.name)
            parts = [subject, result.summary.strip()]
            if refine is not None:  # refined work goes on top as a new commit, with the note
                parts = [refine_subject(sr.step.id, subject), result.summary.strip(), refine_note_block(refine)]
            message = "\n\n".join(part for part in parts if part)
            try:
                if sr.step.reconciles:  # its artifact alone, and the commit checked to hold exactly that
                    sha = commit_artifact(self.worktree, message, self.cfg, fmt(str(sr.step.output), self.ctx),
                                          expected_head=runner_head(self.run))
                else:
                    sha = commit(self.worktree, message, self.cfg, expected_head=runner_head(self.run))
            except CommitRejected as exc:
                return self._fail(sr, [Violation(exc.kind, str(exc))])
            except NothingToCommit:
                sha = None
            if sha:
                self.run.emit("step_committed", key=sr.key, sha=sha, summary=result.summary)
        extra = {"tree": tree} if tree else {}
        self._remember_code_map(sr, result)
        self._remember_lessons_cited(sr, result)
        self.run.emit("step_finished", key=sr.key, status="done", summary=result.summary, sha=sha, **extra)
        return "done"

    def _remember_code_map(self, sr: _StepRun, result: StepResult) -> None:
        """Keep the code map a finished step reported, once per entry (any case). A stopped step never gets here."""
        known = {str(c.get("text")).casefold() for c in self.run.state.get("code_map") or []}
        for entry in result.code_map:
            if entry.casefold() not in known:
                known.add(entry.casefold())
                self.run.emit("code_noted", text=entry, key=sr.key)

    def _remember_lessons_cited(self, sr: _StepRun, result: StepResult) -> None:
        """One `lesson_cited` per lesson a finished step cites that the step's latest `lessons_injected` carried;
        an id that was not injected is ignored. A stopped step never gets here."""
        if not result.lessons_cited:
            return
        injected = [e for e in self.run.events.read() if e["type"] == "lessons_injected" and e.get("key") == sr.key]
        carried = set(injected[-1].get("ids") or []) if injected else set()
        for lesson_id in dict.fromkeys(result.lessons_cited):
            if lesson_id in carried:
                self.run.emit("lesson_cited", key=sr.key, id=lesson_id)

    def _soft_noted(self, soft: float) -> bool:
        """The run already said it passed this soft budget: a soft limit notifies once per value, and plan sizing
        raising it is a new value."""
        return any(e["type"] == "budget_soft_reached" and e.get("soft_usd") == soft for e in self.run.events.read())

    @staticmethod
    def _refused_flag_summary(sr: _StepRun, flag: str | None) -> str:
        """The CLI refused a flag before the session started: name it and the way out. No retry fixes it. A flag the
        runner passes means a CLI too old for it, so the key that drops it comes first and the other key second (the
        same CLI refuses that one next); any other flag came from the pack's own args."""
        prefix = f"agents.{sr.agent_name}"
        start = f"The `{sr.agent_name}` CLI refused the flag `{flag}` before the session started"
        user_settings = f"`{prefix}.user_settings: true` (the pack or your user file) drops `{NO_USER_SETTINGS[0]}`"
        lean = f"`{prefix}.lean: false` drops {' and '.join(f'`{arg}`' for arg in LEAN_ARGS)}"
        if flag in LEAN_ARGS:
            first, second = lean, user_settings
        elif flag == NO_USER_SETTINGS[0]:
            first, second = user_settings, lean
        else:
            return (f"{start}, and the runner doesn't pass it on its own: check `{prefix}.base_args` and "
                    f"`{prefix}.effort_arg` for it, and whether this version of the CLI takes it.")
        return (f"{start}: run sessions need Claude Code {CLAUDE_MIN_VERSION} or later. Update the CLI, or turn the "
                f"flag off: {first}, and {second}.")

    def _stop(self, sr: _StepRun, kind: str, summary: str, extra: str = "") -> Outcome:
        raise_hitl(self.cfg, self.run, kind, sr.key, summary, extra=extra)
        return "stopped"

    def _outside_output(self, sr: _StepRun) -> list[Violation]:
        """For a `reconciles` step: what in the tree is more than an edit of its `output`, against the step's baseline
        (`gitops.outside_only`). A step with no `output` has nothing it may edit, and one with no baseline has nothing
        to be checked against: both are violations too."""
        if not sr.step.output:
            return [Violation("outside_output", f"reconcile step {sr.step.id} has no output, so it may edit nothing")]
        if sr.reconcile_base is None:
            return [Violation("outside_output", f"reconcile step {sr.step.id} has no baseline to be checked against")]
        return outside_only(self.worktree, fmt(str(sr.step.output), self.ctx), sr.reconcile_base)

    def _reconcile_start(self, sr: _StepRun, fresh: bool) -> list[Violation]:
        """The baseline a reconcile step is checked against, then the check itself.

        The git state (`gitops.git_state`) is the one recorded at the step's first start in the run, kept across
        resumes, approvals and rounds: an approved stop never adopts a changed config, filter, attribute file or
        replace ref. Only a human's `agentflow retry` of the step (`step_retry`) takes a new one. The flagged index
        entries and the ignored paths are this attempt's: a resumed step keeps them, so a runner that died mid-turn
        never adopts what that turn changed, and a fresh start or a human's approval reads them again (the human looked
        at the tree). A git state that differs from the kept one is refused before those lists are read, since reading
        them runs `git status`."""
        original: dict | None = None
        recorded: dict | None = None
        for event in self.run.events.read():
            if event.get("key") != sr.key:
                continue
            if event["type"] == "step_retry":
                original = recorded = None
            elif event["type"] == "hitl_approved":
                recorded = None
            elif event["type"] == "reconcile_baseline" and isinstance(event.get("baseline"), dict):
                original = original or event["baseline"].get("git_state")
                recorded = event["baseline"]
        branch = self.run.state.get("branch")
        if recorded is not None and not fresh:
            sr.reconcile_base = {**recorded, "git_state": original or recorded.get("git_state")}
            return self._outside_output(sr)
        if original is not None:
            changed = git_state_changes(self.worktree, {"branch": branch, "git_state": original})
            if changed:
                return changed
        try:
            base = reconcile_baseline(self.worktree, branch)
        except (GitError, OSError, ValueError) as exc:
            return [Violation("outside_output", f"cannot read the work tree's state, so it cannot be trusted: {exc}")]
        if original is not None:
            base["git_state"] = original
        sr.reconcile_base = base
        self.run.emit("reconcile_baseline", key=sr.key, baseline=base)
        return self._outside_output(sr)

    def _fail(self, sr: _StepRun, violations: list[Violation]) -> Outcome:
        """A guard tripped: the step fails with no retry, and a human looks at the tree."""
        summary = f"Guard violation in {sr.key}: {_violation_text(violations)}"
        if sr.step.reconciles and any(v.kind == "outside_output" for v in violations):
            summary += (". Remove or revert the file(s) named here before you approve or reject: a later step commits "
                        "the whole work tree.")
        if any(v.kind == "read_only_edit" for v in violations):
            summary += f". {READ_ONLY_REVERT}"
        self.run.emit("guard_tripped", key=sr.key, violations=[{"kind": v.kind, "detail": v.detail} for v in violations])
        raise_hitl(self.cfg, self.run, "guard_violation", sr.key, summary)
        self.run.emit("step_finished", key=sr.key, status="failed", summary=summary)
        return "stopped"

    # -- prompt pieces

    def _rec(self, sr: _StepRun) -> dict:
        return {**(self.run.state["steps"].get(sr.key) or {}), "agent": sr.agent_name, "role": sr.role}

    def _step_extra(self, key: str, extra: dict | None) -> dict:
        """`extra` plus the result path, written out because a headless agent cannot read its environment."""
        return {**(extra or {}), "result_file": str(self.run.agent_result_path(key)), "phase": key.split("/")[0]}

    def _values(self, sr: _StepRun) -> dict[str, str]:
        return _values(self.cfg, sr.step, self.ctx, sr.extra)

    def _prompt(self, sr: _StepRun, resume: str | None = None) -> str:
        """The step's prompt for a turn that resumes session `resume`, or for a fresh session when it is None.

        A fresh session (the first turn, a rollover, a refine round whose session is gone)
        holds nothing yet, so it is never built as a continuation: the skill's text goes in again."""
        step = sr.step if resume else dataclasses.replace(sr.step, continuation=False)
        if resume is None:  # decided before the prompt is built: the prompt leaves out what the brief holds
            self._decide_read_only(sr)
            self._decide_brief(sr)
            self._set_read_only_note(sr)
        prompt = build_prompt(self.cfg, step, self.ctx, self._rec(sr), sr.extra, sr.mode, brief=sr.brief,
                              self_contained=self._self_contained(sr))
        return f"{sr.lead}\n\n{prompt}" if sr.lead else prompt

    # -- read-only roles (`agents.<name>.read_only_roles`)

    def _decide_read_only(self, sr: _StepRun) -> None:
        """A fresh session's read-only setting (`sr.read_only`): its role is one of the agent's `read_only_roles`, the
        agent takes the setting, and the allow rule can name the workspace exactly. A workspace path it can't name runs
        the step as before, never read-only, and says so once per run (`read_only_skipped`); a dry run says nothing."""
        sr.read_only = False
        spec = self.cfg.agents.get(sr.agent_name)
        if spec is None or sr.role not in spec.read_only_roles or not getattr(sr.agent, "takes_read_only_role", False):
            return
        try:
            read_only_allow([self.run.agent_dir])  # the rules `agents.argv` builds: it raises the same way
        except ValueError as exc:
            if not (self.dry_run or sr.dry) and not any(
                    e["type"] == "read_only_skipped" for e in self.run.events.read()):
                self.run.emit("read_only_skipped", key=sr.key, reason=str(exc))
            return
        sr.read_only = sr.read_only_seen = True

    def _set_read_only_note(self, sr: _StepRun) -> None:
        """A fresh read-only session's `{read_only_note}`: the note (its `{progress}` filled in here, because a value's
        own placeholders are never filled) and the table of the run's gate results to date when it has any. Any other
        fresh session, or a resumed turn, has none: a resumed session was told at its start."""
        extra = {k: v for k, v in sr.extra.items() if k != "read_only_note"}
        if sr.read_only:
            note = fmt(READ_ONLY_NOTE, {"progress": self._values(sr)["progress"]})
            table = results_table(self.run.events.read())
            extra["read_only_note"] = f"{note}\n\n{table}" if table else note
        sr.extra = extra

    def _load_read_only(self, sr: _StepRun, row: dict, session: str) -> None:
        """A resumed session's read-only setting, from its step row (`agent_started.read_only`), whatever the config
        says now. A session from before the setting, or a row whose setting belongs to another session, is False."""
        sr.read_only = row.get("read_only") is True and row.get("read_only_session") == session
        sr.read_only_seen = sr.read_only_seen or sr.read_only

    def _guard_read_only(self, sr: _StepRun) -> bool:
        """The tree guard's `read_only` (`check_step`): this session is read-only, or an earlier session of the step
        since its baseline was (`read_only_seen`), whatever a fresh session decided since. The argv and the prompt
        follow `sr.read_only` alone."""
        return sr.read_only or sr.read_only_seen

    def _self_contained(self, sr: _StepRun) -> bool:
        """The call gets no skills listing (`--disable-slash-commands`), so its prompt names skill files, not skills: a
        read-only session, a reconcile step and the record step."""
        record = self.cfg.record_step
        return sr.read_only or sr.step.reconciles or (record is not None and sr.step.id == record.id)

    def _read_only_edits_left(self, since: list[dict], tree: str) -> list[str]:
        """The paths the step's last `read_only_edit` violation named that `tree` still has changed against the start
        of that failed session (its `step_started`, the first of `since`). A restart must not adopt them."""
        if not since or not since[0].get("start_tree"):
            return []
        named: list[str] = []
        for event in since:
            if event["type"] == "guard_tripped":
                found = [str(v.get("detail")) for v in event.get("violations") or []
                         if isinstance(v, dict) and v.get("kind") == "read_only_edit"]
                named = found or named
        if not named:
            return []
        try:
            still = set(changed_paths(self.worktree, str(since[0]["start_tree"]), tree))
        except (subprocess.CalledProcessError, OSError):  # the start tree can't be read: every named path counts
            return named
        return [path for path in named if path in still]

    # -- the run brief

    def _decide_brief(self, sr: _StepRun) -> None:
        """A fresh session's brief: built and written (`sr.brief`, `sr.brief_text`) when this executor gives briefs,
        `context.run_brief` is on and the agent takes one; a brief that can't be built or written gives none, and
        the reason goes on the call's `agent_started` (`sr.brief_skipped`). A dry run builds it and writes nothing."""
        sr.brief = sr.brief_text = sr.brief_skipped = sr.brief_invalid = None
        if not (self.briefs and self.cfg.context.run_brief and getattr(sr.agent, "takes_brief", False)):
            return
        if sr.brief_refused:
            sr.brief_skipped = sr.brief_refused
            return
        built = self._build_brief(sr, sr.phase.name, sr.role)
        if isinstance(built, str):
            sr.brief_skipped = built
            return
        text, pin = built
        if not (self.dry_run or sr.dry):
            why = self._write_brief(pin, text)
            if why:
                sr.brief_skipped = why
                return
        sr.brief, sr.brief_text = pin, text
        self._brief_texts[pin["sha256"]] = (text, pin)

    def _build_brief(self, sr: _StepRun, phase_name: str, role: str) -> tuple[str, dict] | str:
        try:
            body = _template(self.cfg, sr.step.prompt or DEFAULT_TEMPLATE).strip()
        except (OSError, UnicodeError) as exc:
            return f"the step's template could not be read: {exc}"[:BRIEF_REASON_MAX]
        return build_brief(self.cfg, self.run, phase_name, sr.step, role, body, self._values(sr))

    def _write_brief(self, pin: dict, text: str) -> str | None:
        """Write the brief file afresh (whatever is at its path is replaced, never followed); why it failed, or None."""
        try:
            safeio.ensure_dir(self.run.dir / BRIEF_DIR)
            safeio.replace_file(Path(str(pin["path"])), text.encode("utf-8"))
        except OSError as exc:
            return f"the brief file could not be written: {exc}"[:BRIEF_REASON_MAX]
        return None

    def _load_pin(self, sr: _StepRun, row: dict, session: str) -> None:
        """A resumed session's pin, from its step row in state; its text when this executor wrote it, else rebuilt
        before the call. A pin that is malformed (`_pin_ok`) or bound to another session than `session` (the row's
        `brief_session`) is refused at the call (`sr.brief_invalid`), which then starts a fresh session."""
        sr.brief = _pin(row.get("brief"))
        sr.brief_invalid = None
        if sr.brief is not None and not _pin_ok(sr.brief):
            sr.brief_invalid = "the pinned brief is malformed"
        elif sr.brief is not None and row.get("brief_session") != session:
            sr.brief_invalid = "the pinned brief belongs to another session"
        cached = self._brief_texts.get(str(sr.brief.get("sha256"))) if sr.brief is not None else None
        sr.brief_text = cached[0] if cached is not None and _same_flags(cached[1], sr.brief) else None

    def _ensure_brief(self, sr: _StepRun) -> str | None:
        """Before every call of a session with a pin: the pinned file holds exactly the pinned text. The text is the
        one kept in memory, or after a restart `build_brief` run again for the pinned phase and role; it must have the
        pinned digest and flags, and the pinned path must be this run's brief file named by that digest. Why the call
        can't have it, or None."""
        pin = sr.brief
        if pin is None:
            return None
        if sr.brief_invalid is not None:
            return sr.brief_invalid
        try:
            phase, role, sha = str(pin["phase"]), str(pin["role"]), str(pin["sha256"])
            path = Path(str(pin["path"]))
        except (KeyError, TypeError, ValueError):
            return "the pinned brief is malformed"
        text = sr.brief_text
        if text is None:
            built = self._build_brief(sr, phase, role)
            if isinstance(built, str):
                return f"the brief could not be rebuilt: {built}"[:BRIEF_REASON_MAX]
            text, rebuilt = built
            if not _same_flags(rebuilt, pin):
                return "the rebuilt brief does not hold what its pin says"
        if brief_mod.digest(text) != sha:
            return "the brief does not match its pinned digest"
        if path != self.run.dir / BRIEF_DIR / brief_mod.file_name(phase, role, text):
            return "the pinned path is not this run's brief file"
        why = self._write_brief(pin, text)
        if why:
            return why
        sr.brief_text = text
        self._brief_texts[sha] = (text, pin)
        return None

    def _replace_brief(self, sr: _StepRun, turn: _Turn, why: str) -> _Turn:
        """The session's brief can't be given to its next call (a template changed between a shared session's tasks,
        an engine upgrade, a tampered file): a fresh session, which records a new pin, as `_rollover` does. A fresh
        session whose own brief failed this way goes on without one, so this never loops. A resumed turn's own message
        (a human's or the runner's answers, an approval or reopen note, a fix turn's gate output) goes after the fresh
        prompt, under `BRIEF_CARRIED`, so nothing it said is lost; a fresh turn's prompt is built again whole."""
        self.run.emit("brief_replaced", key=sr.key, reason=why, session_id=turn.resume)
        if turn.resume is None:
            sr.brief_refused = why
        old_lead = self._fresh_session(sr)
        values = self._values(sr)
        lead = BRIEF_LEAD.format(progress=values["progress"], handoff=values["handoff"])
        prompt = f"{lead}\n\n{self._prompt(sr)}"
        if turn.resume is not None:
            carried = turn.prompt.strip()
            if old_lead and carried.startswith(old_lead):  # the fresh prompt has the step's lead already, whole
                carried = carried[len(old_lead):].strip()
            prompt = f"{prompt.rstrip()}\n\n{BRIEF_CARRIED}\n\n{carried}\n"
        return _Turn(prompt, None)

    def _fresh_session(self, sr: _StepRun) -> str:
        """The step goes on in a fresh session (a rollover, a refine round whose session is gone, a replaced brief).
        That session has seen nothing, so a lead built with the gate excerpt for a resumed session is built again with
        the whole report. Returns the lead it had before."""
        old = sr.lead
        sr.session = None
        if sr.lead_fix is not None:
            failed, note = sr.lead_fix
            sr.lead = f"{self._fix_lead(sr, failed, 1, excerpt=False)}{note}"
        return old

    def _result_partial(self, sr: _StepRun) -> str:
        """What a resume turn ends with: the one-line reminder, which names the result file. A session with no brief
        has no `_result.md` in its system prompt, so the contract follows the reminder."""
        text = RESULT_REMINDER if sr.brief is not None else f"{RESULT_REMINDER}\n\n{_template(self.cfg, RESULT).strip()}"
        return fmt(text, self._values(sr)).strip() + "\n"

    def _refine_prompt(self, sr: _StepRun) -> str:
        """A refine round that resumes its session: the note and what it needs, no more. The session already holds the
        step (its preamble, head and body, or its brief), so there is no preamble, no step head and no body again, only
        the lead (gate output, when the round starts in a fix), the refine template filled in, the overlay's skill
        lines (the overlay may have become active since the first round) and the result reminder."""
        rec = self._rec(sr)
        template = _template(self.cfg, self.cfg.phase_approval.get("refine_prompt") or DEFAULT_REFINE).strip()
        values = {**self._values(sr), **_refine_values(self.ctx, rec)}
        parts = [fmt(template, values)]
        skills = overlays.skill_lines(self.cfg, self.ctx, sr.role, parts[0])
        if skills and self._self_contained(sr):  # the call has no skills listing: the lines name the files
            skills = _read_file_skills(skills, self.cfg.project.skills_dir,
                                       resolve=_skill_path(self.ctx.repo, self.cfg.project.skills_dir))
        if skills:
            parts.append(skills)
        parts.append(self._result_partial(sr).strip())
        text = "\n\n".join(parts) + "\n"
        return f"{sr.lead}\n\n{text}" if sr.lead else text

    def _continue_prompt(self, sr: _StepRun, answers: dict[str, str], approved: dict | None = None) -> str:
        if answers:
            lines = "\n".join(f"{qid}: {text}" for qid, text in answers.items())
            who = "The runner took the option you recommended for each question" if self._answered_by_runner(sr.key) \
                else "A human answered your questions"
            lead = f"{who}:\n\n{lines}\n\nCarry on with the step using these answers."
        elif approved is not None and approved["type"] == "step_reopened":
            lead = str(approved.get("note") or "The runner reopened this step: carry on with it.")
        elif approved is not None:
            progress = self._values(sr)["progress"]
            note = f"Their note: {approved['note']}\n\n" if approved.get("note") else ""
            lead = (
                f"The runner stopped this step ({approved.get('kind')}: {approved.get('summary') or 'no summary'}). "
                f"A human looked at it and approved going on. {note}Carry on where you left off: "
                f"check the work tree and `{progress}`, then finish the step. If a command was denied earlier, "
                "try it once more, because the rules may have changed. If it is denied again, stop as `blocked` "
                "and name the exact command."
            )
        else:
            progress = self._values(sr)["progress"]
            lead = (
                "The runner stopped while this step was running. Carry on where you left off: "
                f"check the work tree and `{progress}`, then finish the step."
            )
        return f"{lead}\n\n{self._result_partial(sr)}"

    def _answered_by_runner(self, key: str) -> bool:
        answered = [e for e in self.run.events.read() if e["type"] == "hitl_answered" and e.get("key") == key]
        return bool(answered) and answered[-1].get("by") == AUTO_BY

    def _reminder_prompt(self, sr: _StepRun, error: str) -> str:
        return (
            f"The runner found no valid result.json for this step ({error}). "
            "If the step is finished, write the result file now; otherwise finish the step first.\n\n"
            f"{self._result_partial(sr)}"
        )

    def stop_report(self, key: str) -> str:
        """What the resolver is told beyond the stop's summary: the inbox page a human would have read."""
        path = self.run.inbox_path(key)
        report = path.read_text(encoding="utf-8") if path.is_file() else "(no report)"
        earlier = self._earlier_resolver_findings(key)
        return f"{report.rstrip()}\n\n{earlier}" if earlier else report

    def _earlier_resolver_findings(self, key: str) -> str:
        """The last few resolver summaries the run has for steps other than `key`, so a second resolver does not
        start from nothing. A summary is model text: one line, cut."""
        found = [(e.get("key"), " ".join(str(e.get("summary") or "").split())[:RESOLVER_FINDING_MAX])
                 for e in self.run.events.read() if e["type"] == "resolve_finished" and e.get("key") != key]
        shown = [f"- `{k}`: {summary}" for k, summary in found if summary][-RESOLVER_FINDINGS_SHOWN:]
        return "### Earlier resolver findings in this run\n\n" + "\n".join(shown) if shown else ""

    def _denied_report(self, key: str, read_only: bool = False) -> str:
        """The inbox section naming what was denied since the last approval, each once, so one fix covers it. A
        `read_only` step's denials are by design, so no rule is suggested for them."""
        seen: dict[str, None] = {}
        for event in _after_approval(self.run.events.read(), key):
            if event["type"] == "permission_denied" and event.get("key") == key:
                tool_input = event.get("input") if isinstance(event.get("input"), dict) else {}
                target = tool_input.get("command") or tool_input.get("file_path") or ""
                seen[f"{event.get('tool')}: {str(target)[:DENIED_TEXT_MAX]}"] = None
        if not seen:
            return ""
        shown = list(seen)[:DENIED_SHOWN]
        more = f"\n- and {len(seen) - len(shown)} more" if len(seen) > len(shown) else ""
        lines = "\n".join(f"- `{line}`" for line in shown)
        if read_only:
            return f"### Denied in this step\n\n{lines}{more}\n\n{READ_ONLY_DENIED}"
        rules = self.worktree / ".claude" / "settings.local.json"
        return (f"### Denied in this step\n\n{lines}{more}\n\nTo allow one in this run, add a narrow rule to `{rules}`. "
                "The session reads the worktree's settings, and a change to `.claude/settings.json` there is a guard "
                "violation. To keep it for later runs, add it to `.claude/settings.json` on the main checkout.")

    def _gate_report(self, failed: list[GateResult]) -> str:
        return failure_report(failed)

    def _fix_lead(self, sr: _StepRun, failed: list[GateResult], attempt: int, excerpt: bool) -> str:
        """The fix turn's lead. A session that has seen this step gets `excerpt`: the gate output cut to
        `context.resume_gate_lines`, each gate's log path kept. A step that starts fresh gets the whole report."""
        lead = (
            f"The gates failed (fix attempt {attempt} of {self.cfg.defaults.fix_attempts}). "
            "Fix the cause in this session: never weaken a check, skip a test or add a suppression. "
            "Run the failing gates yourself, then write the result file again."
        )
        report = resume_report(failed, self.cfg.context.resume_gate_lines) if excerpt else self._gate_report(failed)
        return f"{lead}\n\n{report}"

    def _fix_prompt(self, sr: _StepRun, failed: list[GateResult]) -> str:
        return f"{self._fix_lead(sr, failed, sr.fixes, excerpt=True)}\n\n{self._result_partial(sr)}"

    def _uncommitted_forbidden(self, tree: str) -> list[str]:
        """Forbidden paths that differ between `HEAD` and `tree` (changes nobody has committed)."""
        base = head(self.worktree) + "^{tree}"
        patterns = self.cfg.guardrails.forbidden_paths
        return [p for p in changed_paths(self.worktree, base, tree) if any(path_matches(p, g) for g in patterns)]

    # -- event log

    def _baseline(self, key: str, since: list[dict], started: dict) -> tuple[str, str] | list[Violation]:
        """`(start_head, start_tree)` the guards compare a resumed step against, or the violations that
        make the step fail before any call.

        The recorded start, unless the runner committed other work since (another task while this one
        was parked, a recorded decision): then `HEAD` and the tree as they are now (`step_rebased`), so
        the runner's own commits never count as the agent moving `HEAD`. A rebase is refused when a
        commit since the start is not one the runner recorded (an agent committed), or when the tree
        holds an uncommitted change to a forbidden path: adopting either would hide it from the guards.
        """
        head_sha = started.get("start_head") or head(self.worktree)
        tree = started.get("start_tree") or snapshot(self.worktree)
        rebased = 0
        for event in since:
            if event["type"] == "step_rebased" and event.get("key") == key:
                head_sha, tree, rebased = event["start_head"], event["start_tree"], event["seq"]
        others = [e for e in since if e["type"] == "step_committed" and e.get("key") != key and e.get("sha")
                  and e["seq"] > rebased]
        if not others:
            return head_sha, tree
        ours = {e["sha"] for e in self.run.events.read()
                if e["type"] in RUNNER_COMMITS and e.get("sha")}
        try:
            moved = commits_between(self.worktree, head_sha)
        except GitError as exc:
            return [Violation("agent_commit", f"cannot tell what moved HEAD since {head_sha[:12]}: {exc}")]
        foreign = [sha for sha in moved if sha not in ours]
        if foreign:
            shown = ", ".join(sha[:12] for sha in foreign)
            return [Violation("agent_commit", f"HEAD moved by commit(s) the runner did not make: {shown}")]
        now_tree = snapshot(self.worktree)
        dirty = self._uncommitted_forbidden(now_tree)
        if dirty:
            return [Violation("forbidden_path", path) for path in dirty]
        now_head = head(self.worktree)
        self.run.emit("step_rebased", key=key, start_head=now_head, start_tree=now_tree,
                      reason=f"the runner committed {len(others)} other change(s) since the step started")
        return now_head, now_tree

    def _since_step_started(self, key: str) -> list[dict]:
        """Events from the step's last `step_started` on (empty when it never started)."""
        events = self.run.events.read()
        for index in range(len(events) - 1, -1, -1):
            if events[index]["type"] == "step_started" and events[index].get("key") == key:
                return events[index:]
        return []
