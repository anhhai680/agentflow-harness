"""Which of the policy's obligations apply to a run: its lane, and its `when` condition on the final diff."""

from __future__ import annotations

from collections.abc import Callable

from agentflow.conditions import Ctx, evaluate
from agentflow.config import Config, Obligation, Step

ALWAYS = "always"


def applicable(cfg: Config, ctx: Ctx, lane: str) -> list[tuple[Obligation, str]]:
    """The obligations that hold for a run of `lane`, each with why: `always`, or the condition's text.

    An obligation with `lanes` applies to those lanes only. One with a `when` applies unless the condition is
    False: an unknown result (git cannot diff) counts as holding, so the check fails safe. The floor has neither,
    so it is always in, and it comes last, as it does in `cfg.policy.obligations`.
    """
    found: list[tuple[Obligation, str]] = []
    for obligation in cfg.policy.obligations.values():
        if obligation.lanes and lane not in obligation.lanes:
            continue
        if obligation.when is None:
            found.append((obligation, ALWAYS))
            continue
        result = evaluate(cfg, obligation.when, ctx)
        if result.value is not False:
            found.append((obligation, result.reason))
    return found


def phase_where(cfg: Config, holds: Callable[[Step], bool]) -> str | None:
    """The first phase with a step, at any nesting, for which `holds` is true."""

    def walk(steps: list[Step]) -> bool:
        return any(holds(step) or walk(step.steps) or (step.review is not None and holds(step.review)) for step in steps)

    return next((name for name, phase in cfg.phases.items() if walk(phase.steps)), None)


def step_phase(cfg: Config, step_id: str) -> str | None:
    """The phase whose steps, at any nesting, hold `step_id`; None when no phase does."""
    return phase_where(cfg, lambda step: step.id == step_id)
