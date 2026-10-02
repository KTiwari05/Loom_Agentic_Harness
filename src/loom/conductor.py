"""The Conductor — Loom's cost-aware model router (heuristic v1).

Routing rules (see docs/05-models-and-cost-routing.md):
- critic role   -> the reviewer model (LOOM_MODEL_CRITIC)
- escalation    -> the strong model (LOOM_MODEL_STRONG) after a failed step
                   (tool error, failed tests, or a critic rejection)
- otherwise     -> the default workhorse (LOOM_MODEL)

It also prices the "what if every token ran on a frontier model" baseline, which powers
the live "$ saved" meter. v2 (a learned difficulty classifier trained on Loom's own
traces) is Phase 7 — see the roadmap.
"""

from __future__ import annotations

from .config import Config


class Conductor:
    def __init__(self, config: Config):
        self.config = config

    def pick_model(self, role: str = "implementer", escalate: bool = False) -> str:
        if role == "critic":
            return self.config.model_critic
        if escalate:
            return self.config.model_strong
        return self.config.model

    def baseline_cost(self, usage: dict[str, int]) -> float:
        """What this call would have cost on the frontier-priced baseline."""
        return (
            usage.get("prompt_tokens", 0) * self.config.baseline_input_per_m
            + usage.get("completion_tokens", 0) * self.config.baseline_output_per_m
        ) / 1_000_000
