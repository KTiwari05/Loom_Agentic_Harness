"""The Critic — a second model reviews the turn's diff before Loom declares success.

The verifier that makes a stochastic model behave like a dependable tool: when the
implementer says "done" and files changed, the Critic sees the goal + the unified diff and
either APPROVEs or sends it back with concrete feedback (bounded rounds, escalated model).
See docs/02-novel-features.md (F2).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import models

_CRITIC_PROMPT = """You are a strict senior code reviewer inside an autonomous coding agent.

The user's goal:
{goal}

The agent claims the goal is complete. Here is the full diff of every file it changed this turn:

{diff}

Review ONLY for: (1) does the diff actually accomplish the goal, (2) obvious bugs or broken
syntax, (3) edits that clearly damage unrelated code. Do not nitpick style.

Reply in EXACTLY one of these two forms:
APPROVE
REVISE: <one short paragraph of concrete, actionable feedback>"""


@dataclass
class Verdict:
    approved: bool
    feedback: str
    cost: float
    usage: dict[str, int]
    model: str
    raw: str = ""  # the model's verbatim reply — recorded so replay can reproduce the verdict


def review(goal: str, diff: str, model: str, completer=None) -> Verdict:
    """`completer` defaults to the live model; replay injects recorded responses instead."""
    complete = completer or models.complete
    resp = complete(
        model,
        [{"role": "user", "content": _CRITIC_PROMPT.format(goal=goal, diff=diff or "(empty diff)")}],
        tools=None,
        temperature=0.0,
    )
    text = resp.text.strip()
    upper = text.upper()
    if upper.startswith("APPROVE"):
        return Verdict(True, "", resp.cost, resp.usage, resp.model, raw=resp.text)
    feedback = text.partition(":")[2].strip() if upper.startswith("REVISE") else text
    return Verdict(
        False,
        feedback or "The change does not accomplish the goal.",
        resp.cost,
        resp.usage,
        resp.model,
        raw=resp.text,
    )
