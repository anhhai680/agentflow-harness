"""The security overlay: what a run on a security path adds to the lanes it walks.

The overlay holds when the `security_path` condition does (changed paths or plan keywords; see
`config.yaml`'s `overlays.security`). It then:

- appends `Also use the `<skill>` skill.` to the prompt of every step whose role has an entry in
  `overlays.security.add_skills` (plan and review), unless the prompt already names that skill;
- runs the `security_review` step with `--agent <overlays.security.review_agent>`.

`touches_security` asks the same of one plan task's own files (`when_paths` only), so the plan sizer
raises only that task, not the whole plan.

An unknown `security_path` (git cannot diff, say) counts as holding for the prompt line, which only
asks for more care; the `security_review` step decides it through its own `when`. The offer of
`/doubt-driven-development` is the `plan` phase's `approval_note` in `config.yaml`, not an overlay matter.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from agentflow.conditions import Ctx, evaluate
from agentflow.guards import path_matches

if TYPE_CHECKING:
    from agentflow.config import Config, Step

OVERLAY = "security"
CONDITION = "security_path"
REVIEW_STEP = "security_review"

def _overlay(cfg: Config) -> dict:
    found = cfg.overlays.get(OVERLAY)
    return found if isinstance(found, dict) else {}


def security_path(cfg: Config, ctx: Ctx) -> bool:
    """Whether the overlay applies: `security_path` is true, or unknown (more care costs nothing)."""
    if CONDITION not in cfg.conditions:
        return False
    return evaluate(cfg, CONDITION, ctx).value is not False


def touches_security(cfg: Config, paths: Iterable[str]) -> bool:
    """Whether any of `paths` matches `overlays.security.when_paths`: one plan task's own files, where
    `security_path` is the run's (its changed paths and the whole plan's keywords)."""
    globs = _overlay(cfg).get("when_paths") or []
    globs = [globs] if isinstance(globs, str) else [str(g) for g in globs]
    return any(path_matches(path, glob) for path in paths for glob in globs)


def skill_lines(cfg: Config, ctx: Ctx, role: str, body: str) -> str:
    """The `Also use ...` line(s) the overlay adds for `role`, or "" when it adds none."""
    skills = _overlay(cfg).get("add_skills") or {}
    wanted = skills.get(role)
    names = [wanted] if isinstance(wanted, str) else list(wanted or [])
    names = [name for name in names if f"`{name}` skill" not in body]
    if not names or not security_path(cfg, ctx):
        return ""
    return "\n".join(f"Also use the `{name}` skill." for name in names)


def agent_profile(cfg: Config, step: Step) -> str | None:
    """`--agent <review_agent>` for the overlay's `security_review` step; None for every other step."""
    if step.id != REVIEW_STEP:
        return None
    profile = _overlay(cfg).get("review_agent")
    return str(profile) if profile else None
