"""Runtime configuration, loaded from environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _load_user_env() -> None:
    """Settings saved by the desktop app (~/.loom/loom.env) apply to the CLI too.
    Loaded AFTER .env with setdefault, so a project-local .env always wins."""
    user_env = Path.home() / ".loom" / "loom.env"
    if not user_env.is_file():
        return
    for line in user_env.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_user_env()


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    """All knobs in one place. Override via env vars (see .env.example) or CLI flags."""

    model: str = field(default_factory=lambda: os.getenv("LOOM_MODEL", "openrouter/moonshotai/kimi-k2"))
    # Escalation tier: used when a step failed (tool error / critic rejection). Defaults to `model`.
    model_strong: str = field(default_factory=lambda: os.getenv("LOOM_MODEL_STRONG", ""))
    # Reviewer tier: used by the Critic. Defaults to `model`.
    model_critic: str = field(default_factory=lambda: os.getenv("LOOM_MODEL_CRITIC", ""))
    temperature: float = field(default_factory=lambda: float(os.getenv("LOOM_TEMPERATURE", "0.2")))
    max_steps: int = field(default_factory=lambda: int(os.getenv("LOOM_MAX_STEPS", "30")))
    max_cost_usd: float = field(default_factory=lambda: float(os.getenv("LOOM_MAX_COST_USD", "1.0")))
    auto_approve: bool = field(default_factory=lambda: _env_bool("LOOM_AUTO_APPROVE"))
    workdir: str = field(default_factory=os.getcwd)
    # Subagent nesting depth (0 = the main agent). Set internally by tools/agents.py;
    # subagents don't get a `task` tool of their own, so agents can't recurse.
    agent_depth: int = 0

    # The Critic: review file changes against the goal before declaring the task done.
    critic_enabled: bool = field(default_factory=lambda: _env_bool("LOOM_CRITIC", default=True))
    max_critic_rounds: int = field(default_factory=lambda: int(os.getenv("LOOM_MAX_CRITIC_ROUNDS", "2")))

    # Baseline for the "$ saved" meter: what the same tokens would cost on a frontier model
    # (USD per 1M tokens). Defaults ≈ frontier-class pricing; tune in .env.
    baseline_input_per_m: float = field(
        default_factory=lambda: float(os.getenv("LOOM_BASELINE_INPUT_PER_M", "3.0"))
    )
    baseline_output_per_m: float = field(
        default_factory=lambda: float(os.getenv("LOOM_BASELINE_OUTPUT_PER_M", "15.0"))
    )

    def __post_init__(self) -> None:
        self.model_strong = self.model_strong or self.model
        self.model_critic = self.model_critic or self.model
